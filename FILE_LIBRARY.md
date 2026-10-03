# 文件库运行说明

## 数据与权限

- 新文件按 UTC 年份分配 `WL-SYBG-YYYY-NNN` 编号；新版本沿用编号，`version` 在同一编号内递增。迁移 `0005_file_library` 会按旧文件创建时间回填编号，旧 ID 和审核状态不变。
- `classification` 为 `public`、`internal` 或 `confidential`。公开文件通过审核后可匿名查看；内部文件需登录；保密文件仅上传者、关联项目成员和管理员可见。项目关联只允许申请人或成员创建。
- 新版本只有上传者能提交，仍需单独审核。列表与站内搜索只展示最新已审核版本；历史版本通过 `GET /files/{id}/versions` 查询。原版在新版本待审时仍可用。
- 驳回只影响被审核的记录；同一 SHA3 对应的其他已通过文件继续可用。只有没有待审或已通过引用时才删除共享对象。
- 公开文件的文件名、编号、说明、标签及提取正文进入 PostgreSQL 全文检索，并可重建到外部索引。保密或待审文件不入公共索引。

## 上传安全与预览

- 同步上传和 S3 直传确认都执行相同的 SHA3 去重、字节上限与安全处理。生产 Compose 默认启动 ClamAV 并配置 `clamav:3310`，通过 `INSTREAM` 扫描；扫描器不可用或发现威胁时拒绝登记。本地单进程运行时可设置 `LKM_FILES_CLAMAV_ADDRESS=host:3310`，空地址表示未启用扫描。ClamAV 的 `StreamMaxLength` 应至少等于 `LKM_MAX_UPLOAD_BYTES`。
- `LKM_FILES_SENSITIVE_TERMS` 为逗号分隔词表，检查文件名、描述和可提取正文。文本、PDF 与 Word/PowerPoint/ODF 正文可提取；图片、压缩包与加密文档不能保证正文过滤。词表为空表示未启用此规则。
- 镜像安装 LibreOffice 和 Poppler。50 MB 内的 Word/PowerPoint/ODF 文档在上传时转成 PDF，`/preview` 返回转换后的 PDF；原文件下载不变。PDF 正文同样提取供搜索。调整 `LKM_FILES_PREVIEW_MAX_BYTES` 可改转码上限。转换失败时预览返回 415，文件仍可审核及下载。
- 公开文件的预览可匿名访问；内部和保密文件的预览依照文件密级鉴权。附件下载继续走下载权限。

## 备份与归档

- `LKM_FILES_BACKUP_DIR` 指向独立持久卷。审核通过前复制并校验 SHA3；未成功则不通过审核。每日 `backup_approved_files` 任务逐批补齐旧有已审核文件，`backed_up_at` 记录完成时间。生产 Compose 将备份写入 `files_backup_data` 卷；还需对该卷做异机备份和恢复演练，单机卷无法防主机损坏。
- `LKM_FILES_ARCHIVE_DIR` 与 `LKM_FILES_ARCHIVE_AFTER_DAYS` 控制本地或 S3 文件的低频归档。每日任务在所有存活引用都过期、已完成配置的备份后，将哈希对象迁到独立归档目录并清理热对象；预览和下载可从归档目录流式读取，S3 已归档文件的下载地址会回退到后端流。生产 Compose 将归档写入独立的 `files_archive_data` 卷。API 与任务进程必须挂载同一路径。
- 归档卷和备份卷都在当前主机；生产灾备仍需异机备份与恢复演练。若使用具备自动生命周期管理的外部对象存储，可关闭应用归档目录并由对象存储统一管理低频层。

执行数据库迁移后再启动新版 API 与任务进程；切换外部检索引擎时运行 `python -m app.flows.search_reindex` 重建索引。
