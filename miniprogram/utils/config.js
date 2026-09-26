// ============ 只配置「本业务后端」地址 ============
// 上线要求：https + 域名已备案，例如 https://api.你的域名
// 本地调试：http://你的服务器IP:8000，并在开发者工具勾选「不校验合法域名」
//
// 注意：这里不要出现 Hermes 地址、Hermes 密钥或任何大模型 Key。
// 小程序只与本服务通信，Hermes 由服务端在内部网络调用。
const BASE_URL = 'https://api.你的域名';

module.exports = { BASE_URL };
