# 第三方客户端兼容

Neo 使用单管理员账号。MoviePilotLite 等原版客户端依赖的只读身份字段和用户查询接口予以保留。

- `POST /api/v1/login/access-token` 和 PassKey 登录响应包含 `super_user` 与 `permissions`。权限字典提供 `discovery`、`search`、`subscribe`、`manage`，由认证后的管理员身份派生。
- `GET /api/v1/user/current`、`GET /api/v1/user/{user_name}` 返回当前账号资料，包含 `is_active`、`is_superuser` 和 `permissions`。未设置的邮箱返回空字符串。用户名必须与当前登录账号一致，否则返回 404。
- `GET /api/v1/user/` 返回仅含当前管理员的数组，供旧客户端读取账号资料。
- 上述用户接口仍要求有效的管理员身份；权限字段仅用于客户端展示，不代替服务端鉴权。新增、删除用户及旧版用户修改接口保持关闭；自助修改资料使用 `PUT /api/v1/user/current`，拒绝提交身份或权限字段。

升级后，已缓存旧登录响应的客户端可重新登录或刷新账号资料。此兼容层修复发现、搜索、订阅和管理入口误判无权限的问题，不包含旧版多用户管理功能。
