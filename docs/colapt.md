# Cola 站点配置

在「站点管理」中新增站点：

- 站点地址：`https://www.colapt.com/`。
- 认证方式选 API，在 **API Key** 字段填写站点「Key 管理 → API Key」创建的密钥，并启用「兼容面（MoviePilot）」。
- Authorization / Token 与 Cookie 留空；不使用 Pass Key 或 RSS Key 替代 API Key。
- 保存后执行连接测试；无需将私有站点设置为公开。

主程序会使用 `https://api.colapt.com` 的 M-Team 兼容接口。支持关键词搜索、最新资源浏览、
订阅搜索、下载票换取与种子文件读取、账户和做种统计；字幕接口为空时按无字幕处理。
RSS 仍需自行配置该站点的 RSS 地址，本适配不会根据 API Key 拼接 RSS Key。

v2 官方资源目录尚未收录 Cola，因此在 `app/helper/site.py` 补充站点定义，
各索引入口统一读取该目录。正常用户认证、流控与已配置站点的启停保持生效；
上游收录后优先使用上游配置，不重复添加，也不修改二进制资源文件。

站点当前返回整数 `code: 0`；M-Team 的字符串 `code: "0"` 同样接受。
搜索业务失败不能被误判成成功但无结果，连接测试通过 profile 接口验证 API Key，
不通过首页 HTML 判断 Cookie 登录态。

适配依据为站内 [MoviePilot 对接说明](https://www.colapt.com/account/tracker?tab=moviepilot)，
于 2026-10-16 验证。离线测试样本仅保留资源字段，不包含 API Key、账户资料或临时下载票。
