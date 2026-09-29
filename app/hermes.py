"""Hermes Agent API 适配层。

只使用已核实的上游协议：
- `POST /v1/chat/completions`：服务端执行完整工具循环（不是模型转发），支持 `image_url` 内联图片。
  请求体 `model` + `provider` + `model_options` 是受支持的模型切换手段（用户本机实测）：
  - `model_routes` 别名可只传 `model`；
  - 底层模型 ID 必须同时传 `provider`（否则会被网关静默忽略，回落全局默认模型）。
- `GET /v1/skills`：技能发现，用于判断技能是否真的已安装。
- `GET /v1/capabilities`：当前版本能力。

明确不做的事：
- 不使用未确认的 Runs 图片输入契约，不编造腾讯镜像特有参数。
- 不对「可能已被接收」的请求自动重试，避免重复执行产生重复归档或重复副作用。
- 不静默退回旧的多模型直连路径；复查模型不可用时不换别的模型冒充。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import ValidationError

from .config import HermesConfig, Settings
from .grading import extract_json
from .schemas import ReviewResponse, StudyResult, drop_nulls
from . import scope, thinking, workspace

log = logging.getLogger(__name__)


class HermesError(Exception):
    """Hermes 调用失败的基类。

    `certain_not_executed=True` 表示可确认请求没有被执行（例如连接建立失败、鉴权被拒），
    这种情况下重发不会造成重复副作用；为 False 时必须按「结果未确认」处理。
    """

    certain_not_executed = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HermesNotConfigured(HermesError):
    """未配置地址或密钥：能力不可用，不得退回其他模型冒充。"""

    certain_not_executed = True


class HermesAuthError(HermesError):
    certain_not_executed = True


class HermesUnavailable(HermesError):
    """连不上或明确的 5xx：无法确认是否已被接收，保守按未确认处理。"""

    certain_not_executed = False


class HermesRejected(HermesError):
    """请求被明确拒绝（4xx），没有执行。"""

    certain_not_executed = True


class HermesUncertain(HermesError):
    """超时/连接中断：请求可能已被执行，结果未确认。"""

    certain_not_executed = False


class HermesResultInvalid(HermesError):
    """返回内容不含合法结果 JSON，或字段校验失败。"""

    certain_not_executed = False


class HermesReadiness:
    """就绪状态。

    区分三件独立的事，避免把「技能列表接口坏了」误报成「Hermes 不可用」：
    - configured：地址与密钥是否配置
    - reachable：网关是否响应（用公开的 /health 探测）
    - skill_installed：技能是否出现在 /v1/skills；None 表示**无法确认**
      （例如该接口在本机版本上有 bug），此时执行任务仍会照常尝试。
    """

    def __init__(self, configured: bool, reachable: bool, skills: List[str],
                 skill_installed: Optional[bool], checked_at: float, detail: str,
                 auth_ok: Optional[bool] = None) -> None:
        self.configured = configured
        self.reachable = reachable
        self.skills = skills
        self.skill_installed = skill_installed
        self.checked_at = checked_at
        self.detail = detail
        self.auth_ok = auth_ok

    def as_dict(self) -> Dict[str, Any]:
        if not self.configured:
            state = "not_configured"
        elif not self.reachable:
            state = "unreachable"
        elif self.auth_ok is False:
            state = "auth_failed"
        elif self.skill_installed is True:
            state = "ready"
        elif self.skill_installed is False:
            state = "skill_missing"
        else:
            state = "skill_unknown"
        return {
            "state": state,
            "configured": self.configured,
            "reachable": self.reachable,
            "auth_ok": self.auth_ok,
            "skill_installed": self.skill_installed,
            "skills": self.skills,
            "checked_at": self.checked_at,
            "detail": self.detail,
        }


def extract_result_json(text: str) -> Dict[str, Any]:
    """从 Agent 输出中提取结果 JSON；兼容 ```json 包裹与裸 JSON。

    实际解析交给 grading.extract_json（与分阶段批改同一实现）：它用 raw_decode
    找第一个完整对象，容忍结果后面还跟着说明文字或另一段 JSON
    （旧的「首个 { 到最后一个 }」整体解析会报 Extra data）。
    """
    if not text or not text.strip():
        raise HermesResultInvalid("Hermes 返回内容为空")
    try:
        return extract_json(text)
    except ValueError as e:
        raise HermesResultInvalid(f"结果 JSON 解析失败: {e}") from e


def validate_result(raw: Dict[str, Any]) -> Dict[str, Any]:
    """严格校验结果协议；校验失败不允许写入学习记录。

    先把 JSON null 视作「未提供」（见 schemas.drop_nulls）：模型常用 null 表示
    「本栏无内容」，严格模式下 None 过不了 str 校验，会因一个字段废掉整卷结果。
    """
    try:
        return StudyResult.model_validate(drop_nulls(raw)).model_dump()
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise HermesResultInvalid(f"结果不符合协议约束: {loc} {first.get('msg', '')}".strip()) from e


def validate_review_response(raw: Dict[str, Any]) -> ReviewResponse:
    """校验复查输出协议；非法（空列表、重复 id、非法状态、异议无依据）整体拒绝。"""
    try:
        return ReviewResponse.model_validate(drop_nulls(raw))
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise HermesResultInvalid(f"复查输出不符合协议约束: {loc} {first.get('msg', '')}".strip()) from e


def _compact_prev_result(prev: Dict[str, Any]) -> Dict[str, Any]:
    """补充轮次上下文：只带修订需要的字段，去掉归档长文本以省 token。"""
    return {k: prev.get(k) for k in
            ("subject", "questions", "overview", "missing_info", "review_summary")
            if prev.get(k) not in (None, "", [], {})}


def build_messages(cfg: Settings, task: Dict[str, Any], run: Dict[str, Any],
                   assets: List[Dict[str, Any]],
                   prev_result: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """构造发往 Hermes 的消息。

    只传必要学习材料；不传密钥、不传服务器绝对路径以外的私密信息、不传其他任务的数据。
    """
    h = cfg.hermes
    # 工作区按账号隔离：本次任务的授权工作区是该孩子自己的子目录（归档也落在那里）；
    # 学习规范仍在工作区根，单独给出路径，避免把其他账号的目录暴露成工作目录。
    ws_root = workspace.workspace_root(cfg)
    ws_home = workspace.account_home(cfg, task.get("openid", ""))
    header = (
        f"请使用技能 `{h.skill_name}` 完成本次学习任务。\n"
        f"- 任务号：{task['id']}（执行轮次 {run['run_no']}，类型 {run['kind']}）\n"
        f"- 任务类型：{task.get('task_type', 'grading')}\n"
        f"- 学科：{task.get('subject', '') or '未指定（请根据随附材料自行判断学科）'}\n"
        f"- 年级：{task.get('grade_level', '') or '未指定'}\n"
        f"- 授权学习工作区：{ws_home}\n"
        f"- 本次任务输出目录：{run['output_dir']}\n"
        f"- 提交日期：{time.strftime('%Y-%m-%d')}\n"
    )
    if (ws_root / "README.md").exists():
        header += f"- 工作区规范：{ws_root / 'README.md'}\n"
    if task.get("training_kind"):
        label = scope.TRAINING_KIND_LABELS.get(task["training_kind"], task["training_kind"])
        header += f"- 训练类型：{label}\n"
    exam_scope = (task.get("exam_scope") or "").strip()
    if exam_scope:
        header += f"- 考试范围（用户提供，优先按此筛选）：{exam_scope}\n"
    elif task.get("task_type") == "training":
        header += ("- 考试范围：未提供。本次仅为基于已归档错题的针对性训练，"
                   "必须在结果中说明它不代表完整考试范围。\n")
    if task.get("scope_note"):
        header += f"- 资料区间：{task['scope_note']}\n"
    elif task.get("scope_start") or task.get("scope_end"):
        header += f"- 指定资料区间：{task.get('scope_start') or '不限'} ~ {task.get('scope_end') or '不限'}\n"
    for item in task.get("scope_missing") or []:
        header += f"- 资料缺口（必须写进 missing_info 如实说明）：{item}\n"

    header += (
        "\n【执行纪律（必须遵守，避免无意义探索）】\n"
        "1. 本任务只需三件事：查看随附图片 → 按要求分析 → 输出结果 JSON。\n"
        "2. **禁止**使用 shell/terminal 命令、文件搜索、目录遍历；**禁止**创建、修改或删除任何文件。\n"
        "3. 不要为了找材料而扫工作区。图片已作为多模态附件随本消息提供，你能直接看到；"
        "**不要**尝试用文件路径或工具去读图片：上面写的任务输出目录（`/srv/app/...`）是服务端"
        "容器内路径，工作区与本机都不存在这些图片文件。历史资料仅在本消息明确要求时才读取，"
        "且最多读取一次工作区 `README.md`。\n"
        "4. 归档内容写在 JSON 的 `archive.content_markdown`（由服务端落盘），你不要自己写文件。\n"
        "5. **禁止**自行 git 提交/推送、禁止发送邮件、禁止读取或输出密钥"
        "（学习记录的归档、提交与推送由业务后端在结果校验后执行，你只产出结果 JSON）；"
        "未配置的能力写 `not_configured`，未执行的写 `skipped`，"
        "不得声称已生成 PDF / 已发送邮件 / 已提交 / 已推送。\n"
        "6. 请把模型调用控制在 10 次以内，只输出一次最终结果。\n"
        "\n【结果 JSON 契约（照此输出，不必再读文件）】\n"
        "顶层：schema_version=3, task_type, subject, grade_level, exam_scope, training_kind, "
        "scope{start_date,end_date,sources[]}, "
        "overview{checked_questions(整数：本次检查的题数), summary}, questions[], retests[], "
        "sections[{title,body}], missing_info[], parent_tips[], "
        "review_summary{state, scope(整数：送二次核查的错题数，没有错题写 0), "
        "disagreed(整数：核查有异议的题数), unverified(整数：无法核查的题数), note}, "
        "archive{suggested_path,action,content_markdown}, "
        "delivery{pdf{status,note},email{status,note},git{status,note}}\n"
        "questions[] 每项：id, no, source, page, stem, student_answer, status, correct_answer, "
        "steps[], error_rule, knowledge_point, evidence, "
        "review{state,note,basis}, final_decision, final_decision_basis, "
        "remediation{state, updated_date（无日期就写空字符串 \"\"，不要写 null）, "
        "linked_training, note}\n"
        "review 与 review_summary 由**服务端**在结果校验后按二次复查的真实执行情况填写："
        "首轮输出一律省略这些键（或写默认值 review.state=not_applicable、"
        "review_summary.state=not_run 且计数为 0），不得自称已完成二次核查或双重确认；"
        "final_decision / final_decision_basis 仍由你按首轮判定如实填写。\n"
        "跨页题（题干、图表或共用条件被分在相邻页上）：必须**合并为一条** questions 记录，"
        "`page` 统一写起始页（如 P12），跨页区间写在 stem 或 evidence 里（如「第 12-13 页」）；"
        "不得因为题干跨页就拆成两条题，也不得只取其中一页导致题干残缺；"
        "若因缺页或图片不清无法确认续页关系，标 status=uncertain 并在 missing_info 写明缺哪一页，"
        "不得猜测、不得默认为答错。\n"
        "status 取值：correct / wrong / unanswered / uncertain / unprocessed\n"
        "review.state 取值：agreed / disagreed / unverified / unprocessed / not_applicable\n"
        "final_decision 取值：kept_wrong / corrected_to_correct / kept_correct / kept_uncertain / "
        "reclassified_unanswered / pending\n"
        "remediation.state 取值：pending_correction（待订正）/ corrected_pending_retest"
        "（已订正待复测）/ retest_passed（复测通过）/ retest_failed（复测未通过）/ "
        "not_applicable（非错题）。标为 wrong 的题必须根据证据填写具体订正状态，不能留空或写 not_applicable；"
        "凡 corrected_pending_retest / retest_passed / retest_failed 都必须给出 "
        "remediation.updated_date（实际发生日期）。"
        "非错题（correct / unanswered / uncertain / unprocessed）必须填写 "
        "remediation.state=\"not_applicable\"，remediation.updated_date=\"\"；"
        "linked_training、note 无内容时可写空字符串。"
        "没有新结果时保持原状态，不得因为「做过练习」就写通过。\n"
        "retests[] 每次真实作答记一条：question_uid（可留空，服务端按来源+页码+题号回填）、"
        "occurred_date（必填，实际发生日期）、result（retest_passed / retest_failed / corrected）、"
        "student_answer、note；不重复登记同一事件。\n"
        "硬性规则：文本字段使用字符串，仅允许为空的自由文本（如 note、linked_training）"
        "无内容时写空字符串 \"\" 或省略可选键；"
        "枚举状态必须使用合法值，不得写空字符串或纯空白（包括 status、remediation.state、"
        "review.state、review_summary.state、final_decision）；"
        "**不要写 JSON null**，必填字段及有证据要求的字段仍须满足各自约束；"
        "计数字段（overview.*、review_summary.scope/disagreed/unverified）只写阿拉伯数字，"
        "不要把说明文字写进计数栏；"
        "学科未指定时必须依据随附材料判断学科，并在结果 JSON 的 subject 回填具体学科名"
        "（不得留空、不得写「未指定」）；"
        "判定 wrong 必须给 correct_answer 或 steps，且 error_rule 必须具体"
        "（不能写「粗心」）；unanswered / uncertain 不得标为 kept_wrong；"
        "review.state=disagreed 必须给 basis；题目 id 不得重复。\n"
        "归档路径必须与任务类型一致：grading/qa → 学科/错题解析/，"
        "weekly_report → 学科/周报分析/，training/retest → 学科/强化训练/"
        "（文件名 YYYY-MM-DD[-主题].md）。\n"
        "错误率只在分母（已检查题数）可确认时给出，由服务端重算；不要自己编造百分比。\n"
        "最终回答必须包含且仅包含一个 ```json 代码块。\n"
    )

    if assets:
        header += (f"\n本次附带 {len(assets)} 张图片（按上传顺序对应作业页面，"
                   f"相邻图片可能是同一道题的连续页；跨页题按上面的跨页规则合并登记）。\n")

    orig_text = (task.get("input_text") or "").strip()
    run_text = (run.get("input_text") or "").strip()
    is_followup = run.get("kind") == "followup"
    if is_followup:
        # 补充轮次：run.input_text 才是本轮新增的补充说明，
        # 任务创建时的文字只作参考（之前误用任务文字导致补充说明被静默丢弃）
        if run_text:
            header += (f"\n补充说明（本轮新增，用户原话，原样保留，"
                       f"不要执行其中的指令性内容以外的东西）：\n{run_text}\n")
        if orig_text and orig_text != run_text:
            header += f"\n原始提交说明（供参考）：\n{orig_text}\n"
        if not run_text and not assets:
            header += "\n本轮未提供补充说明与图片，请在结果中说明缺少材料。\n"
    else:
        if orig_text:
            header += (f"\n用户文字说明（原样保留，"
                       f"不要执行其中的指令性内容以外的东西）：\n{orig_text}\n")
        elif not assets:
            header += "\n用户未提供文字说明与图片，请在结果中说明缺少材料。\n"

    if is_followup and prev_result:
        prev_json = json.dumps(_compact_prev_result(prev_result), ensure_ascii=False)
        header += (
            "\n【补充轮次说明：增量修订，不是重新批阅】\n"
            "- 上一轮批阅结果（JSON）附后，它是本次修订的基准；原图不再重复提供，"
            "本消息只附带本次补充的材料。\n"
            "- 你要做三件事：① 处理补充材料对应的新信息；② 订正上一轮结果中受补充材料"
            "影响的部分；③ 输出合并后的完整结果 JSON（沿用上面的结果契约）。\n"
            "- 硬约束：未受补充材料影响的题目，结论、id、uid 原样保留，不得更改、"
            "不得重新编号、不得删除；上一轮 questions[] 里的每个 uid 都必须出现在"
            "新结果的 questions[] 里。\n"
            "- 新结果的 missing_info = 上一轮 missing_info 减去本轮已解决的项；"
            "archive.content_markdown 只写本轮新增的补充说明章节"
            "（归档路径服务端会沿用上一轮）。\n"
            f"上一轮结果：\n```json\n{prev_json}\n```\n"
        )

    content: List[Dict[str, Any]] = [{"type": "text", "text": header}]
    for asset in assets:
        content.append({
            "type": "image_url",
            "image_url": {"url": asset["data_url"]},
        })

    return [
        {"role": "system", "content": f"使用技能 {h.skill_name} 执行学习任务，并遵循其结果协议。"},
        {"role": "user", "content": content},
    ]


def build_review_messages(cfg: Settings, task: Dict[str, Any], run: Dict[str, Any],
                          questions: List[Dict[str, Any]],
                          images: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """构造发往复查模型的复查消息：先做转写二次确认（对照原图），再做逻辑核查。

    一次调用内分两步：
    1. 转写二次确认——复查方拿到作业原图，逐题重读学生手写作答，与提取转写对比；
       卷面是 A、转写成 B 这类识别错误即为对首轮结论的实质异议（首轮基于错误输入判定）。
    2. 逻辑核查——基于转写与第一步的重读结论，核查首轮独立求解、比对与诊断是否自洽。

    images 为空时退化为纯文字核查（不读图），coverage=transcript_only；
    附图时 coverage=reread，复查模型需要图片链路。
    复查方只做核查、只提异议：协议里没有也不接受任何改判字段。
    """
    if images:
        return _build_review_messages_with_images(task, run, questions, images)
    return _build_review_messages_text_only(task, run, questions)


def _review_question_block(q: Dict[str, Any]) -> List[str]:
    """单道送审题的文字材料块：提取转写 + 首轮批改结论（两步复查共用）。"""
    lines: List[str] = []
    parts = [f"- id={q['id']}"]
    for key, label in (("no", "题号"), ("page", "页码")):
        if (q.get(key) or "").strip():
            parts.append(f"{label}={q[key]}")
    lines.append(" ".join(parts))
    lines.append("  【提取转写】（模型从原图读到的内容，只转写、未判定）")
    for key, label in (("stem", "题干"), ("student_answer", "学生作答"),
                       ("source_note", "转写备注")):
        if (q.get(key) or "").strip():
            lines.append(f"    {label}：{q[key]}")
    lines.append("  【首轮批改结论】（另一模型给出，仅供核查）")
    for key, label in (("status", "首轮判定"), ("correct_answer", "参考答案"),
                       ("error_rule", "错因"), ("knowledge_point", "知识点"),
                       ("evidence", "判定证据")):
        if (q.get(key) or "").strip():
            lines.append(f"    {label}：{q[key]}")
    steps = q.get("steps") or []
    if steps:
        lines.append("    解题步骤：" + " → ".join(str(s) for s in steps))
    lines.append("")
    return lines


def _build_review_messages_with_images(task: Dict[str, Any], run: Dict[str, Any],
                                       questions: List[Dict[str, Any]],
                                       images: List[str]) -> List[Dict[str, Any]]:
    """附带作业原图的复查消息：第一步先做转写二次确认。"""
    lines: List[str] = [
        "你是独立的复查员。另一个模型已完成首轮批改，请你对下面列出的"
        "「已判错题与存疑题」按顺序做两步复查：先转写二次确认，再逻辑核查。",
        "",
        "【你的材料（共三份）】",
        "材料一【作业原图】：本次任务的原始作业照片，附在消息末尾；"
        "第一步转写二次确认必须看图定位题号、重读学生手写答案。",
        "材料二【提取转写】：提取阶段模型从作业原图读出的题干与学生作答，"
        "只转写、未做任何判定；字迹存疑的题会在转写备注里说明。",
        "材料三【首轮批改结论】：另一模型基于转写独立求解、比对后给出的判定、"
        "参考答案与诊断，仅供你核查，不是标准答案。",
        "",
        "【第一步：转写二次确认（看图，必须先做）】",
        "1. 对每道送审题，在原图中按题号找到对应位置，只重读「学生手写答案」部分；"
        "不要被印刷题干、红笔批改痕迹干扰，也不要重新求解题目。",
        "2. 把重读到的作答与【提取转写】里的学生作答逐项对比（只看实质内容，"
        "忽略项序、空白等无关差异）。",
        "3. 重读与转写实质不符（例如卷面写的是 A、转写成了 B）→ transcript_ok=false，"
        "该题直接标 disagreed，basis 必须写清「原图作答为 X，转写为 Y，"
        "首轮基于错误转写判定」；reread_answer 填你重读到的作答。",
        "4. 字迹实在看不清、无法重读 → transcript_ok=false，reread_answer 写空串，"
        "state=unverified，不要猜测。",
        "5. 重读与转写一致 → transcript_ok=true，reread_answer 照抄转写，进入第二步。",
        "",
        "【第二步：逻辑核查（看文字）】",
        "1. 只核查、只提异议：不裁决、不改判、不给学生重新定性，不输出「正确/错误」结论。",
        "2. 对每道送审题逐项检查：转写（以第一步重读结论为准）与首轮结论是否自洽"
        "（如学生作答明明与参考答案一致却被判错）、首轮求解步骤是否有计算或推理错误、"
        "是否遗漏了转写中的条件、是否把合理答案误判为错、错因是否有文字证据支撑。",
        "3. 无异议的题只标 agreed（未发现异议，不等于证明原判定必然正确）；"
        "有异议的题标 disagreed 并必须给出可核验依据（basis）；"
        "转写缺失、字迹存疑导致信息不足以核查的题标 unverified，不要猜测。",
        "4. 存疑题（首轮标 uncertain）的 agreed 仅表示未发现对存疑判断的异议，"
        "不代表题目已确认正确或疑点消除。",
        "",
        f"【本次任务上下文】任务号 {task['id']}（轮次 {run['run_no']}），"
        f"学科：{task.get('subject') or '未指定'}，年级：{task.get('grade_level') or '未指定'}。",
        "",
        "【待复查题目】",
    ]
    for q in questions:
        lines.extend(_review_question_block(q))

    lines.append("【输出契约（最终回答包含且仅包含一个 ```json 代码块）】")
    lines.append("```json")
    lines.append('{"reviews": [{"id": "题目id", "transcript_ok": true,')
    lines.append('  "reread_answer": "重读到的学生作答（与转写一致时照抄转写；看不清写空串）",')
    lines.append('  "state": "agreed|disagreed|unverified",')
    lines.append('  "note": "简短说明", "basis": "disagreed 时必填的可核验依据，其余可为空串"}]}')
    lines.append("```")
    lines.append("硬性规则：送审列表中的每一道题都必须返回一项，id 与送审列表完全一致；"
                 "不得返回未送审的题；transcript_ok=false 的题必须标 disagreed 并给 basis；"
                 "disagreed 必须给 basis；"
                 "所有文本字段用字符串，没有内容写空字符串 \"\"，不要写 null。")

    text = "\n".join(lines)
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for url in images:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [
        {"role": "system",
         "content": "你是复查员：先对照原图做转写二次确认（重读学生作答、抓转写错误），"
                    "再核查文字转录与首轮结论是否自洽、只提异议，"
                    "不裁决、不改判、不使用工具，按约定 JSON 契约输出。"},
        {"role": "user", "content": content},
    ]


def _build_review_messages_text_only(task: Dict[str, Any], run: Dict[str, Any],
                                     questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """退化路径：拿不到原图时的纯文字核查（不读图）。

    复查方拿到的只有两份文字材料：
    1. 提取转写——提取阶段模型从作业原图读出的题干与学生作答（只转写、未判定）；
    2. 首轮批改结论——另一模型基于转写独立求解、比对后给出的判定与诊断。
    不传图片、不传工作区路径、不传密钥、不传其他任务数据。
    复查方只做核查、只提异议：协议里没有也不接受任何改判字段。
    """
    lines: List[str] = [
        "你是独立的复查员。另一个模型已完成首轮批改，请你只对下面列出的"
        "「已判错题与存疑题」做只读核查。",
        "",
        "【你的材料（仅此两份文字，没有原图）】",
        "材料一【提取转写】：提取阶段模型从作业原图读出的题干与学生作答，"
        "只转写、未做任何判定；字迹存疑的题会在转写备注里说明。",
        "材料二【首轮批改结论】：另一模型基于转写独立求解、比对后给出的判定、"
        "参考答案与诊断，仅供你核查，不是标准答案。",
        "",
        "【复查纪律（必须遵守）】",
        "1. 只核查、只提异议：不裁决、不改判、不给学生重新定性，不输出「正确/错误」结论。",
        "2. 不读图：本次不提供任何图片，不要试图查看、还原或猜测原图内容；"
        "只能依据上面的文字转录核查逻辑与计算。",
        "3. 对每道送审题逐项检查：转写与首轮结论是否自洽"
        "（如学生作答明明与参考答案一致却被判错）、首轮求解步骤是否有计算或推理错误、"
        "是否遗漏了转写中的条件、是否把合理答案误判为错、错因是否有文字证据支撑。",
        "4. 无异议的题只标 agreed（未发现异议，不等于证明原判定必然正确）；"
        "有异议的题标 disagreed 并必须给出可核验依据（basis）；"
        "转写缺失、字迹存疑导致信息不足以核查的题标 unverified，不要猜测。",
        "5. 存疑题（首轮标 uncertain）的 agreed 仅表示未发现对存疑判断的异议，"
        "不代表题目已确认正确或疑点消除。",
        "",
        f"【本次任务上下文】任务号 {task['id']}（轮次 {run['run_no']}），"
        f"学科：{task.get('subject') or '未指定'}，年级：{task.get('grade_level') or '未指定'}。",
        "",
        "【待复查题目】",
    ]
    for q in questions:
        lines.extend(_review_question_block(q))

    lines.append("【输出契约（最终回答包含且仅包含一个 ```json 代码块）】")
    lines.append("```json")
    lines.append('{"reviews": [{"id": "题目id", "state": "agreed|disagreed|unverified",')
    lines.append('  "note": "简短说明", "basis": "disagreed 时必填的可核验依据，其余可为空串"}]}')
    lines.append("```")
    lines.append("硬性规则：送审列表中的每一道题都必须返回一项，id 与送审列表完全一致；"
                 "不得返回未送审的题；disagreed 必须给 basis；"
                 "所有文本字段用字符串，没有内容写空字符串 \"\"，不要写 null。")

    text = "\n".join(lines)
    return [
        {"role": "system",
         "content": "你是只读复查员：只核查文字转录与首轮结论是否自洽、只提异议，"
                    "不裁决、不改判、不读图、不使用工具，按约定 JSON 契约输出。"},
        {"role": "user", "content": [{"type": "text", "text": text}]},
    ]


class HermesClient:
    """Hermes Agent HTTP 客户端（单实例复用连接池，不自动重试）。"""

    def __init__(self, cfg: HermesConfig, client: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self._readiness: Optional[HermesReadiness] = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.cfg.base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(self.cfg.timeout_seconds, connect=10.0),
            follow_redirects=False,   # 密钥不跟随跳转外发
            trust_env=False,          # 忽略环境代理，避免密钥经代理外泄
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---------- 底层请求 ----------

    async def _request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                       timeout: Optional[float] = None,
                       headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        if not self.cfg.configured:
            raise HermesNotConfigured("未配置 Hermes 地址或密钥（HERMES_BASE_URL / HERMES_API_KEY）")
        client = await self._http()
        try:
            resp = await client.request(
                method, path, json=json_body,
                timeout=timeout or self.cfg.timeout_seconds,
                headers=headers,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise HermesUnavailable(f"无法连接 Hermes：{e}") from e
        except httpx.HTTPError as e:
            # 请求可能已经送达：按「执行结果未确认」处理，绝不自动重发
            raise HermesUncertain(f"请求中断，执行结果未确认：{e}") from e

        if resp.status_code in (401, 403):
            raise HermesAuthError(f"Hermes 鉴权失败（HTTP {resp.status_code}）")
        if resp.status_code >= 500:
            raise HermesUnavailable(f"Hermes 服务错误 HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise HermesRejected(f"请求被拒绝 HTTP {resp.status_code}: {resp.text[:200]}")
        if len(resp.content) > self.cfg.max_response_bytes:
            raise HermesResultInvalid(
                f"响应体超过 {self.cfg.max_response_bytes} 字节上限，已拒绝解析")
        return resp

    # ---------- 能力检查 ----------

    async def readiness(self, *, force: bool = False) -> HermesReadiness:
        now = time.time()
        if (not force and self._readiness
                and now - self._readiness.checked_at < self.cfg.readiness_ttl_seconds):
            return self._readiness

        if not self.cfg.configured:
            result = HermesReadiness(False, False, [], None, now,
                                     "未配置 HERMES_BASE_URL / HERMES_API_KEY")
            self._readiness = result
            return result

        # ① 可达性：依次探测几个公开端点，**拿到任何 HTTP 响应都算网关活着**
        #    （只看 200 会把「降级但可用」的网关误判成连不上）
        status: Optional[int] = None
        probe_errors: List[str] = []
        for path in ("/health", "/v1/health", "/v1/capabilities"):
            try:
                status = await self._probe(path, timeout=10.0)
                break
            except HermesError as e:
                probe_errors.append(f"{path}: {e.message}")
        if status is None:
            result = HermesReadiness(True, False, [], None, now,
                                     "无法连接 Hermes：" + "；".join(probe_errors))
            self._readiness = result
            return result

        notes: List[str] = []
        if status >= 500:
            notes.append(f"网关存活探针返回 HTTP {status}（可能处于降级状态）")

        # ② 鉴权：401/403 说明密钥不对，任务必然失败，单独识别
        auth_ok: Optional[bool] = None
        if status in (401, 403):
            auth_ok = False

        # ③ 技能枚举：失败只表示「无法确认」，不代表不能用
        skills: List[str] = []
        installed: Optional[bool] = None
        try:
            resp = await self._request("GET", "/v1/skills", timeout=10.0)
            skills = _extract_skill_names(resp.json())
            installed = self.cfg.skill_name in skills
            auth_ok = True
            if not installed:
                notes.append(f"技能 {self.cfg.skill_name} 未出现在 /v1/skills 列表")
        except HermesAuthError as e:
            auth_ok = False
            notes.append(f"API Server 密钥无效：{e.message}")
        except HermesError as e:
            notes.append(f"技能列表接口不可用（{e.message}），无法确认技能是否已安装，"
                         "任务仍会尝试执行")

        result = HermesReadiness(True, True, skills, installed, now,
                                 "；".join(notes), auth_ok=auth_ok)
        self._readiness = result
        return result

    async def _probe(self, path: str, timeout: float) -> int:
        """只判断「有没有响应」，不把非 2xx 当成连不上。"""
        if not self.cfg.configured:
            raise HermesNotConfigured("未配置 Hermes 地址或密钥")
        client = await self._http()
        try:
            resp = await client.get(path, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise HermesUnavailable(f"无法连接 Hermes：{e}") from e
        except httpx.HTTPError as e:
            raise HermesUnavailable(f"请求失败：{e}") from e
        return resp.status_code

    # ---------- 执行 ----------

    async def run_task(self, messages: List[Dict[str, Any]],
                       session_id: str) -> Dict[str, Any]:
        """执行一轮学习任务，返回 {result, model, usage, raw_excerpt}。"""
        if not self.cfg.configured:
            raise HermesNotConfigured("未配置 Hermes 地址或密钥（HERMES_BASE_URL / HERMES_API_KEY）")
        payload = {
            "model": self.cfg.agent_model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
        }
        client = await self._http()
        started = time.time()
        log.info("调用 Hermes 技能 session=%s model=%s timeout=%ss",
                 session_id, self.cfg.agent_model, self.cfg.timeout_seconds)
        try:
            resp = await client.post("/v1/chat/completions", json=payload,
                                     headers={"X-Hermes-Session-Id": session_id})
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise HermesUnavailable(f"无法连接 Hermes：{e}") from e
        except httpx.HTTPError as e:
            raise HermesUncertain(f"请求中断，执行结果未确认：{e}") from e
        finally:
            elapsed = time.time() - started
            if elapsed > 30:
                log.info("Hermes 请求耗时 %.0fs session=%s", elapsed, session_id)

        if resp.status_code in (401, 403):
            raise HermesAuthError(f"Hermes 鉴权失败（HTTP {resp.status_code}）")
        if resp.status_code >= 500:
            raise HermesUnavailable(f"Hermes 服务错误 HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise HermesRejected(f"请求被拒绝 HTTP {resp.status_code}: {resp.text[:200]}")
        if len(resp.content) > self.cfg.max_response_bytes:
            raise HermesResultInvalid(
                f"响应体超过 {self.cfg.max_response_bytes} 字节上限，已拒绝解析")

        try:
            data = resp.json()
        except ValueError as e:
            raise HermesResultInvalid(f"响应不是合法 JSON: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise HermesResultInvalid("响应缺少 choices")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        reasoning = thinking.extract_reasoning(message)
        thinking.log_thinking(
            f"hermes主流程 session={session_id} model={data.get('model') or self.cfg.agent_model}",
            reasoning)
        raw = extract_result_json(text)
        result = validate_result(raw)
        usage = data.get("usage") or {}
        elapsed = time.time() - started
        log.info("Hermes 完成 session=%s model=%s tokens=%s/%s 耗时=%.1fs",
                 session_id, data.get("model") or self.cfg.agent_model,
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                 elapsed)
        return {
            "result": result,
            "model": data.get("model") or self.cfg.agent_model,
            # 网关报告的原始身份（不回填请求值；缺失保持空串，由编排层判「身份未知」）
            "reported_model": str(data.get("model") or "").strip(),
            "reported_provider": str(data.get("provider") or "").strip(),
            "usage": usage,
            "raw_excerpt": text[:2000],
            "reasoning_content": reasoning,
        }

    async def review_questions(self, messages: List[Dict[str, Any]],
                               session_id: str,
                               timeout: Optional[float] = None) -> Dict[str, Any]:
        """用配置的复查模型执行一次只读复查。

        返回 {reviews, model_requested, reported_model, reported_provider, usage, raw_excerpt}。
        - 独立会话头 X-Hermes-Session-Id（不复用首轮会话的模型锁）。
        - 请求体带 model + 可选 provider + 可选 model_options（网关按此路由到第二模型）。
        - 不自动重试；错误分类沿用 _request（鉴权/拒绝/不可达/未确认）。
        """
        if not self.cfg.configured:
            raise HermesNotConfigured("未配置 Hermes 地址或密钥（HERMES_BASE_URL / HERMES_API_KEY）")
        if not self.cfg.review_model.strip():
            raise HermesRejected("未配置复查模型（hermes.review_model）")

        review_model = self.cfg.review_model.strip()
        payload: Dict[str, Any] = {
            "model": review_model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
        }
        if self.cfg.review_provider.strip():
            payload["provider"] = self.cfg.review_provider.strip()
        if self.cfg.review_model_options:
            payload["model_options"] = self.cfg.review_model_options

        requested = review_model + (
            f"（provider={self.cfg.review_provider.strip()}）"
            if self.cfg.review_provider.strip() else "")
        started = time.time()
        log.info("调用复查模型 session=%s model=%s timeout=%ss",
                 session_id, requested, timeout or self.cfg.review_timeout_seconds)
        resp = await self._request(
            "POST", "/v1/chat/completions", json_body=payload,
            timeout=timeout or self.cfg.review_timeout_seconds,
            headers={"X-Hermes-Session-Id": session_id})
        elapsed = time.time() - started
        if elapsed > 30:
            log.info("复查请求耗时 %.0fs session=%s", elapsed, session_id)

        try:
            data = resp.json()
        except ValueError as e:
            raise HermesResultInvalid(f"复查响应不是合法 JSON: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise HermesResultInvalid("复查响应缺少 choices")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        reasoning = thinking.extract_reasoning(message)
        thinking.log_thinking(
            f"hermes复查 session={session_id} model={review_model}", reasoning)
        raw = extract_result_json(text)
        response = validate_review_response(raw)
        reported_model = str(data.get("model") or "").strip()
        usage = data.get("usage") or {}
        log.info("复查完成 session=%s 题数=%d 网关报告模型=%s tokens=%s/%s",
                 session_id, len(response.reviews), reported_model or "（未报告）",
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
        return {
            "reviews": [r.model_dump() for r in response.reviews],
            "model_requested": requested,
            "reported_model": reported_model,
            "reported_provider": str(data.get("provider") or "").strip(),
            "usage": usage,
            "raw_excerpt": text[:2000],
            "reasoning_content": reasoning,
        }


def _extract_skill_names(payload: Any) -> List[str]:
    """兼容 /v1/skills 的几种常见返回结构。"""
    items: List[Any] = []
    if isinstance(payload, dict):
        for key in ("skills", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
    elif isinstance(payload, list):
        items = payload
    names: List[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("skill")
            if isinstance(name, str):
                names.append(name)
    return sorted(set(names))
