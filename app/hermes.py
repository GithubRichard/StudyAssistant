"""Hermes Agent API 适配层。

只使用已核实的上游协议：
- `POST /v1/chat/completions`：服务端执行完整工具循环（不是模型转发），支持 `image_url` 内联图片。
- `GET /v1/skills`：技能发现，用于判断技能是否真的已安装。
- `GET /v1/capabilities`：当前版本能力。

明确不做的事：
- 不使用未确认的 Runs 图片输入契约，不编造腾讯镜像特有参数。
- 不对「可能已被接收」的请求自动重试，避免重复执行产生重复归档或重复副作用。
- 不把 `model` 当作底层模型切换手段，不静默退回旧的多模型直连路径。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import ValidationError

from .config import HermesConfig, Settings
from .schemas import StudyResult, drop_nulls
from . import scope, workspace

log = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


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
    """从 Agent 输出中提取结果 JSON；兼容 ```json 包裹与裸 JSON。"""
    if not text or not text.strip():
        raise HermesResultInvalid("Hermes 返回内容为空")
    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(1) if match else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise HermesResultInvalid("Hermes 输出中没有找到结果 JSON")
    try:
        return json.loads(candidate[start:end + 1])
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
        "status 取值：correct / wrong / unanswered / uncertain / unprocessed\n"
        "review.state 取值：agreed / disagreed / unverified / unprocessed / not_applicable\n"
        "final_decision 取值：kept_wrong / corrected_to_correct / kept_correct / kept_uncertain / "
        "reclassified_unanswered / pending\n"
        "remediation.state 取值：pending_correction（待订正）/ corrected_pending_retest"
        "（已订正待复测）/ retest_passed（复测通过）/ retest_failed（复测未通过）/ "
        "not_applicable（非错题）。标为 wrong 的题必须给出具体状态；"
        "凡 corrected_pending_retest / retest_passed / retest_failed 都必须给出 updated_date"
        "（实际发生日期）；非错题这一栏写空字符串 \"\"（不要写 null，也不要省略 state 之外的内容）。"
        "没有新结果时保持原状态，不得因为「做过练习」就写通过。\n"
        "retests[] 每次真实作答记一条：question_uid（可留空，服务端按来源+页码+题号回填）、"
        "occurred_date（必填，实际发生日期）、result（retest_passed / retest_failed / corrected）、"
        "student_answer、note；不重复登记同一事件。\n"
        "硬性规则：所有文本字段一律用字符串——没有内容就写空字符串 \"\" 或直接省略该键，"
        "**不要写 JSON null**（null 会导致整次结果校验失败）；"
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
        header += f"\n本次附带 {len(assets)} 张图片（按顺序对应作业页面）。\n"

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
                       timeout: Optional[float] = None) -> httpx.Response:
        if not self.cfg.configured:
            raise HermesNotConfigured("未配置 Hermes 地址或密钥（HERMES_BASE_URL / HERMES_API_KEY）")
        client = await self._http()
        try:
            resp = await client.request(
                method, path, json=json_body,
                timeout=timeout or self.cfg.timeout_seconds,
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
        raw = extract_result_json(text)
        result = validate_result(raw)
        return {
            "result": result,
            "model": data.get("model") or self.cfg.agent_model,
            "usage": data.get("usage") or {},
            "raw_excerpt": text[:2000],
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
