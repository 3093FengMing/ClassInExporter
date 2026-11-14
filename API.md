# API 与实现导读

既有实测记录针对 ClassIn 6.0.5.4000（Windows x64）。其他版本需要重新核验原生结构，不能仅凭版本号沿用内存布局。

项目采用 [PolyForm Noncommercial 1.0.0](LICENSE)，复制或分发源码及允许的派生作品时须保留 [NOTICE](NOTICE) 中的署名与许可信息。第三方依赖各自保留其许可。许可不授予课程、录播或机构 API 的访问权限。

## 有关标记

| 标记 | 含义 |
| --- | --- |
| 已实测 | 使用当前已登录个人账号完成只读请求，或实际下载并验证文件 |
| 实现 | 已实现并通过合成数据或本地 HTTP / HLS 场景验证，不能视为真实 ClassIn 服务成功 |
| 静态确认 | 公开前端或已安装原生程序的调用链确认路线、字段或解析方式，尚无对应线上成功证据 |
| 未确认 | 当前请求失败、缺少有效上下文，或未取得可验证资源；不编造成功响应 |

## 分析方法与版本适配

1. 从公开 HTML 与 JavaScript 查找真实调用者，再确认 HTTP 包装器的前缀、编码、公共参数、角色与分页逻辑。接口字符串、路由名称或调试 mock 本身不足以证明可用协议。
2. 对已安装 PE 的导出符号和指令进行静态分析，确认会话 getter、签名、原件下载和回放响应解析的调用链。
3. 用明确的合成值比较原生签名结果；随后在当前授权会话中验证最小只读请求，只保留必要字段结构和匿名验证结论。
4. 对下载分别验证原件地址刷新、HTTP 长度、摘要与文件格式；对缓存验证容器、正文范围、完整性与读取期间是否变化。

运行时通过 `PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ` 读取正在运行的 ClassIn 主进程中的已确认对象。

当前 getter 路径从安装文件的导出符号及指令推导，运行时模块基址另行发现。**固定 RVA、虚表偏移和原生对象布局不是通用 HTTP 接口**。适配新版本时，应重新验证导出符号、转发目标、字段读写指令与账号一致性；不符合预期时停止读取，而不是沿用旧地址猜测。

## 源码模块导读

| 模块 | 可以复用的职责 |
| --- | --- |
| [session.py](classin_exporter/session.py) | `load_current_session`、`ClassInSession`、表单 / JSON 签名、当前账号复核、token 更新、服务与静态资源地址 |
| [discovery.py](classin_exporter/discovery.py) | `ClassInClient.scan()`、课程 / 目录分页、资源归一化、缓存关联、`resolve()` 下载前刷新 |
| [lms_material.py](classin_exporter/lms_material.py) | 资料详情、附件列表、`getFilesInfo` 原件、精确大小与附件刷新；可供其他附件适配器复用 |
| [lms_homework.py](classin_exporter/lms_homework.py) | 只读作业详情及附件，复用资料原件流程 |
| [lms_record.py](classin_exporter/lms_record.py) | 录播活动详情、原件与转码播放资源区分、按稳定文件标识刷新 |
| [cache.py](classin_exporter/cache.py) | 只读解析 Qt 磁盘缓存、正文完整性、缓存资源索引 |
| [downloader.py](classin_exporter/downloader.py) | `DownloadManager`、远程优先、完整缓存回退、HLS、取消、摘要与原子完成 |
| [resources.py](classin_exporter/resources.py) | `Resource`、`ScanResult`、`DownloadResult` 数据结构 |
| [grouping.py](classin_exporter/grouping.py) | 课程与云盘分组、稳定目录命名 |
| [gui.py](classin_exporter/gui.py) | Qt 后台扫描、并发批量下载与主线程事件协调 |
| [gui_workspace.py](classin_exporter/gui_workspace.py)、[gui_views.py](classin_exporter/gui_views.py) | 原生 Qt 布局、绘制和交互；独立 HTTP 实现无需依赖界面层 |

## 个人会话认证与签名

`UID` 是当前个人用户；课程请求中的 `SID` / `schoolUid` 是课程所属机构标识。

| 请求 | 编码 / 认证 | 签名方式与约束 |
| --- | --- | --- |
| 个人 Form POST | `application/x-www-form-urlencoded`；`X-EEO-UID`、`X-EEO-SIGN`、`X-EEO-TS` | `generate_form_sign(fields, timestamp, secret)`；时间戳参与签名，不自动写入业务 body |
| 个人 JSON POST | `application/json`；上述头，加 `X-EEO-TOKEN`、`X-EEO-SIGN-VERSION=1` | `generate_json_sign(body_bytes, timestamp)`；签名针对实际发送的紧凑 JSON 字节 |
| token 更新请求 | JSON，使用签名与客户端信息；body 含 `uid` | 当前实现不附加普通业务请求的 UID / access-token 头 |
| CDN 文件 GET | 独立下载授权上下文、`Accept-Encoding: identity` | 不附加个人 API 的签名、token 或设备认证头 |

当前客户端信息包括 `X-APP-ID=classin`、`X-EEO-CLIENT-TYPE=9`、`X-EEO-CLIENT-OS-FLAG=3`、`X-EEO-CLIENT-LANGUAGE=zh_CN`。

表单签名按以下顺序处理：

1. 标量按 Qt QString 语义转为字符串。
2. 替换参与签名的 `key` / `timeStamp`，加入十进制时间戳；值超过 **1024 UTF-8 字节**的字段不参与签名。
3. 按 QString 的 **UTF-16 code unit** 顺序排序字段，使用原始字符串值拼接 `k=v&...`。
4. **最后**追加 `&key=secret`，对 UTF-8 字节计算小写 MD5。secret 不加入字段排序，也不要对值再次 URL 编码后计算 MD5。
5. 业务 body 独立做百分号编码；签名头与实际 body 使用同一组字段和值。

JSON 序列化递归按 QString 键顺序生成无空格 UTF-8 字节，保留非 ASCII 文本并拒绝非有限数字。令 `n=timestamp % 10`：body 长度大于 `n` 时在字节位置 `n` 插入十进制时间戳，否则追加到末尾，再计算小写 MD5。重新序列化、换空白或改变键顺序都会改变签名。

过期 token 优先走 `/usercenter/v2/app/user/refresh-token`，body 为 `uid`、`refreshToken`
缺少 refresh token 时实现 `/zero-usercenter/app/user/tokenExchange`，body 为 `uid`、`token`。
静态解析与实现使用响应字段 `data.token`、`refreshToken`、`expires`、`expTime`。

下面演示真实公开函数。

```python
from classin_exporter.session import generate_form_sign, generate_json_sign

timestamp = 1009
form = {"UID": 42, "folderId": 0, "cate": 2, "order": 0}
form_headers = {
    "X-EEO-UID": "42",
    "X-EEO-TS": str(timestamp),
    "X-EEO-SIGN": generate_form_sign(form, timestamp, "test-secret"),
    "Content-Type": "application/x-www-form-urlencoded",
}
body = b'{"page":1,"pageSize":40,"states":[0]}'
# 实际 JSON 业务请求还须由 ClassInSession 提供当前有效 access token。
json_headers = {
    "X-EEO-UID": "42",
    "X-EEO-TS": str(timestamp),
    "X-EEO-SIGN": generate_json_sign(body, timestamp),
    "X-EEO-SIGN-VERSION": "1",
    "Content-Type": "application/json",
}
example_url = "https://example.invalid/course/app/member/course_list"
```

## 已确认的个人 POST 路线

以下路径相对于当前原生配置的 `dynamic_host`，不是任意猜测的固定域名。认证请求仅允许同源 HTTPS，禁用重定向。当前客户端路线常以 `error_info.errno=1` 表示业务成功；还必须检查 `data` 的真实类型，HTTP 200 不等于业务成功。

### 云盘目录与原件

表单云盘包装器通过 `ClassInClient._post` 加入公共 `UID`。目录响应字段位于 `data`；表格中的“静态确认”不宣称该路线已在当前账号下载成功。

| 行为 / 状态 | POST 路线 | body 主要字段 | 响应与实现 |
| --- | --- | --- | --- |
| 个人目录：已实测 | `/cloudspace/api/user/getFolderList` | `UID`, `folderId=0`, `cate=2`, `order=0`, `resourceFileType=3`, `language=cn`, `appType=1`, `pageInfo` | `topFolderList`, `folderList`, `fileList`, `pageInfo`, `currentAuth`, `sortInfo` |
| 组织磁盘：静态确认 + 实现 | `/cloudspace/api/organization/disk/list` | `UID`, `sid` | `diskList`，行含 `organizationDiskId`, `name` |
| 组织目录：静态确认 + 实现 | `/cloudspace/api/organization/folder/list` | `UID`, `sid`, `organizationDiskId`, `folderId`, `cate=2`, `order=0`, `resourceFileType=3`, `appType=1`, `pageInfo` | `folderList`, `fileList`, `pageInfo`, `schoolInfo` |
| 普通云盘原件：已实测 | `/clouddisk/api/downloadSourceFile` | `UID`, `fileId=userFileId`, `isNew=1` | `data` 字符串或 `data.filePath`；刷新后已完整下载 PDF |
| 课程云盘原件：静态确认 + 实现 | `/clouddisk/api/course/getSourceFilePath` | `UID`, `courseFileId=userFileId`, `courseId`, 当前 `telephone`；有效课次 / 商品加 `clientClassId` / `goodsId` | `data` 字符串或 `data.filePath`；不同于 LMS 附件原件接口 |
| 共享原件：静态确认 + 实现 | `/clouddisk/api/downloadSharedFile` | `UID`, `shareId=referenceId`, `shareUid` | 原件 URL；`fileSource=6, referenceType=6` |
| 组织原件：静态确认 + 实现 | `/cloudspace/api/organization/download` | `UID`, `fileIds=userFileId`, `sid` | `data` 数组，行含 `path`, `fileName`；source 8 或 source 6 / referenceType 8 |
| 课程补充目录：未确认 | `/cloudspace/api/course/getSubFolderList` | `UID`, `courseId`, `folderId` | 当前 `folderId=0` 上下文返回 HTTP 404，未取得成功 schema |

个人与组织目录的初始 `pageInfo` 是 **JSON 字符串字段**：

```json
{"SepIndex":0,"DirIndex":0,"FileIndex":0,"Limit":80,"End":false,"First":true}
```

后续使用服务器返回游标，检测 `End`、重复游标、循环文件夹及读取上限。旧部署可能不返回 `pageInfo`，此时不能编造下一页。当前实现限制 1000 页 / 10000 个文件夹，达到上限要报告不完整。

### 当前课程、分类与课堂课次

以下 JSON 路线实测 `errno=1`。此包装器不自动加入 body `UID`；当前用户身份由认证头提供。

| 行为 | POST 路线 | body | 成功数据字段 |
| --- | --- | --- | --- |
| 当前 / 归档课程 | `/course/app/member/course_list` | `page=1`, `pageSize=40`, `states=[0]` 或 `[1,2]` | `data.list`, `data.total`；行含 `courseId`, `courseName`, `schoolUid`, `identity`, `state` |
| 全部课程分类 | `/lms/app/category/list` | `SID=schoolUid`, `courseId` | `data.list`；`categoryId`, `name`, `orderId` |
| 首次课堂课次 | `/lms/app/course/classActivityList` | `SID`, `courseId`, `categoryId` | `data.notStarted`, `ongoing`, `end`；每组有 `list`, `total` |
| 课次后续页 | 同上 | 上述字段加 `processFlag=0/1/2`, `limit=100`, `lastActivityId` | 对应分组的 `list`, `total` |

课程 `states`：正常 `0`、解散 `1`、离开 `2`。课程列表按 `total` 自增页码；课次分别延续未开始 / 进行中 / 已结束分组，首次不加 `limit`，后续游标取上页末尾 `activityId`。

课次行的 `activityId` 标识 LMS 活动，`bizId` 才是课堂入口使用的 lesson ID。回放 `clientClassId` 应使用这个 lesson ID，不能直接替换为 `activityId`。

### LMS 活动单元与详情

单元、活动目录与下列资料 / 作业 / 录播详情已在当前账号的实际角色分支实测成功；其他角色分支由公开调用链静态确认。教师路径与学生路径按已确认角色选择，不能据单个账号的结果宣称所有角色均已验证。

| 行为 | POST 路线 / 编码 | body | 成功数据字段 |
| --- | --- | --- | --- |
| 单元目录 | `/lms/app/course/unitList` 或 `studentUnitList`，JSON | `SID`, `courseId`, `categoryId`, `role`, `sort=asc`, `isUpcoming=0`, `isSearch=0` | `data.list`, `uuid`；行含 `unitId`, `activityCount`, `activityNum` |
| 单元活动 | `/lms/app/course/unitActivityList` 或 `studentUnitActivityList`，JSON | 上述上下文，`isSearch=1`, `unitIds` 数组，`limit=100`, `offset=0` | `data.list` 为单元 bucket；`unitId`, `activities`, `pageTotal` |
| 资料详情 | `/lms/app/activity/learningMaterials/get`，当前实现 form | `activityId`, `SID`, `login_uid`, `courseId`, 有效 `categoryId`, 详情 `identity` | `data.bizType=5`、`id`；`image`, `audio`, `docs`, `video` 为 JSON 字符串数组 |
| 作业详情 | `/lms/app/activity/homework/get`，form | 同上 | `data.image`, `audio`, `video`, `docs` 附件数组字符串；无附件不等于请求失败 |
| 附件原件信息 | `/lms/app/file/getFilesInfo`，form | `SID`, `login_uid`, `courseId`, 有效 `categoryId`, `appType=1`, `fileIds=JSON.stringify(ids)` | `data` 列表；`file_id`, `file_extension`, `file_save_name`, `file_save_path`, `file_size` |
| 录播详情 | `/lms/app/activity/recordClass/get`，当前实现 JSON | `activityId`, `courseId`, `SID`, `login_uid`, `getStuStatistic=1` | `data.video` 为 JSON 字符串数组；含原件标识、保存路径及转码数据 |

资料详情的 JSON 与 form 请求均已实测成功，但签名必须匹配实际编码；当前资料适配器选择 form。录播适配器使用已实测的 JSON 最小请求，不将公开前端的 form 包装与 JSON 签名混搭。

单元活动首次每批最多 10 个 `unitIds`；随后按各 bucket 的 `pageTotal` 单独请求 `unitIds=[unitId]`、`offset=已读页数*100`。检查缺失 bucket、重复活动、空的未完成页与读取上限，保留已有结果并报告不完整。

详情附件保留 `fileId`, `lmsFileId`, `fileName`, `fileSize`, `filePermission`，有 `linkAddress` 的外链不能假装成可下载原件。`getFilesInfo` 按文件 ID 分批，当前批量上限 100，只接纳请求集合中的响应行。

## 三种角色枚举不能混用

| 语境 | 已确认映射 |
| --- | --- |
| 课程 member `identity` | 学生 `1`，旁听 `2`，教师 `3`，助教 `4`，管理员 `192` |
| LMS 目录 `role` | 课程学生 / 旁听映射 `student`；教师 `teacher`，助教 `teacherAssistant`，管理员 `manager` |
| 资料 / 作业详情 `identity` | 教师侧 `1`，学生侧 `2`；先从课程身份映射到 role，再选择详情枚举 |
| LMS 活动 `type` | 课堂 `1`，作业 `2`，录播 `4`，资料 `5` |

录播前端还存在 `clusterRole` / `classRole`，它们来自另外的上下文枚举。当前最小成功请求未传这两个字段，不把课程 member `identity` 生搬进去，也不发起观看进度、作答或编辑请求。

## 稳定 ID、原件刷新与 CDN 隔离

分别保存 `UID`、`SID`、`courseId`、`activityId`、lesson ID、`userFileId`、`originId`、`fileId`、`lmsFileId` 和引用信息。普通云盘下载使用 `userFileId`；`originId` 不是其替代值。同名不同 ID、不同课程 / 活动的引用各自保留。

资料 ID 由课程、活动和 file ID 组成；录播 ID 同样保留课程、活动与稳定文件 key。下载前调用 `ClassInClient.resolve(resource)`：资料 / 作业仅刷新所选 file ID，录播重读详情并匹配同一文件，课堂回放匹配同一 `FileId`。移除、授权失败、无法匹配或刷新失败都明确报错，不把旧签名 URL 作为刷新成功。

LMS 原件只使用 `file_save_path`；缩略图、预览图及 `transition_data` 都不是原件。绝对 HTTP / HTTPS 地址直接保留；相对路径通过 `ClassInSession.format_static_url` 与当前原生 static CDN 拼接。不要猜 CDN，不把独立统计域名或 CSS 配置名当资源域名，也不要使用会丢失路径前缀的通用 `urljoin` 替代此规则。

`fileSizeNumber` 与部分 `file_size` 是显示或舍入后的 KiB，不能直接当字节长度。资料附件 `fileSize*1024` 只有能明确得到精确字节数时才用于严格校验；不确定时保留未知大小，由下载响应的 `Content-Length` 和可用摘要验证。

API 认证头按请求设置，HTTP transport 不保存全局认证头。下载跨源重定向使用独立干净连接，不转发源站附加的 headers / cookies；只有原资源同源请求使用明确提供的下载授权上下文。线程各自使用连接池，任务结束释放其 transport。

## 录播原件、标准 HLS 与完整缓存

录播优先选择完整 `file_save_path`。缺失时，先使用已确认的 `transition_files + output.V360P.path`，再使用标准 `m3u8`；这些回退明确标记 `content_role=transcoded-playback`、`original_available=false`，不应用原件 MD5。

专有 `pm3u8` 不能按文件扩展名视为标准 HLS。仅有专有播放源时明确失败；当前不实现绕过其加密格式的通用下载。

标准 HLS 实现下载主 / 媒体列表、选择最高带宽变体和相应音轨，下载片段、必要密钥及 URI 引用，改写为本地清单后交 FFmpeg 合并 MP4。媒体列表须有 `#EXT-X-ENDLIST`；检查循环、嵌套和大小上限，逐项验证长度。该流程及短片解码属于 **实现 + fixture**，不代表真实 ClassIn HLS 已完成验证。

直接文件下载检查非空、可用的精确长度和摘要；HTTP 206 片段不发布为完整文件。输出先写临时 `.part`，完成校验后原子发布，同名不覆盖，失败或取消清理未完成输出。

远程下载失败后，仅可回退已确认完整且属于同一正文的缓存。刷新本身失败由调用方报告，不能伪造一条“已刷新”资源进入成功下载流程。

Qt `.d` 缓存是 QNetworkDiskCache 容器，包含元数据与正文。只读解析当前支持的 v8 / QDataStream 13–19 格式，支持受限的 qCompress 解压，只导出正文。当前 profile 来自原生活动缓存 getter；数字目录名不是 UID，不能用最新 mtime 选择另一个账号。

无法读取当前会话时，历史缓存只作为历史候选显示，不能据此宣称已登录或携带历史账号凭据请求远程服务。

完整缓存要求 HTTP 200、非空正文、匹配的长度或可信 MD5，以及读取期间未变化；截断、206、摘要不符、变化中的缓存均拒绝。单独的 m3u8 / pm3u8 文本不能充当完整视频。

缓存与远程引用仅在**完整 URL（含 query）相同或可信 MD5 相同**时关联；不同摘要拒绝合并。录播刷新还核对稳定 ID、原件 / 播放角色和文件标识；完整缓存优先于同正文的片段缓存。

## 当前未确认的能力

课堂回放使用 form POST `/api/classin.api.php?action=getLessonRecordInfo`：`SID`, `clientCourseId`, `clientClassId=lessonId`, `memberUid=currentUid`, `login_uid=currentUid`, `flag=1`。

原生静态解析确认成功记录在 `data.lessonData.fileList`，行含 `StartTime`, `FileId`, `Duration`, `CIDExt`, `Status`，`Playset` 项含 `Definition`, `Url`；实现选择有 HTTP URL 的最高 Definition。**这是静态成功解析证据，当前账号没有对应成功回放下载证据。**

当前回放返回 `error_info.errno=809`。实现只保留 `candidate_only=true`, `availability=denied`, `record_exists=null`、空 URL 的候选入口，并拒绝下载；可见课次和候选入口不能证明录像存在。

课程云盘补充目录在当前 `UID + courseId + folderId=0` 上下文返回 HTTP 404，响应发生于业务 JSON 解析前。未确认正确目录 ID、成功 schema 或失败原因，不能宣称空目录、确定权限错误或服务下线，也不凭空补 `SID` / `rootID`。这些失败不否定独立 LMS 资料、作业与录播详情的成功结果。

## 复用源码实现相似工具

最短路径是复用 `ClassInClient` 做发现与刷新，`Resource` 传递稳定元数据，`DownloadManager` 做完整性与输出。以下代码会使用本人已登录的客户端，仅导出调用者明确选定的资源；使用前先遵守许可与账号权限。

```python
from classin_exporter.discovery import ClassInClient
from classin_exporter.downloader import DownloadManager

client = ClassInClient(max_workers=4)
try:
    scan = client.scan()
    for warning in scan.warnings:
        print(warning)
    selected_id = input("输入本次扫描中要导出的稳定资源 ID：")
    selected = next(item for item in scan.resources if item.id == selected_id)
    fresh = client.resolve(selected)
    result = DownloadManager("exports", session=client.session).download(fresh)
    print(result.source, result.bytes_written, result.sha256)
finally:
    if client.auth_session is not None:
        client.auth_session.close()
```

独立 HTTP 实现应先复现同一种编码的签名与请求头，再接课程 / 目录分页，按活动类型取详情，保持 ID 和角色语义，下载前刷新原件，最后实现 CDN 隔离与完整性。不要对任意嵌套 JSON 递归收集 URL 来替代原件解析。

最小验证顺序是合成签名、已授权的小列表请求、小文件刷新与完整下载，再验证分页、并发、取消和缓存回退。记录成功 / 失败、类型、长度与摘要即可，不把私人响应、真实课程名称、签名地址或凭据写入公开文档。

## 官方 SDK 与机构 API

官方 [SDK 附录](https://docs.eeo.cn/api/zh-hans/appendix/sdklist.html) 链接 [EeoApiSupport/classin-sdk-py](https://github.com/EeoApiSupport/classin-sdk-py)。

`ClassInAPI(school_uid, school_secret)` 接收**学校 SID 与学校密钥**。官方 [对接前 QA](https://docs.eeo.cn/api/zh-hans/Error-Handling/pre-integration.html) 要求先开通机构 API。个人客户端登录、课程的 schoolUid 或个人 app secret 不能代替它。

| 项目 | 官方 SDK / API | 当前个人会话协议 |
| --- | --- | --- |
| v1 签名 | `safeKey=MD5(SECRET+timeStamp)`，body 含机构 `SID` | 原始标量字段排序后追加个人 app secret，签名放头 |
| v2 签名 | 机构 SID、时间戳与 body 标量字段排序，追加 `key=学校密钥`，MD5；`X-EEO-SIGN/UID/TS` | 精确 JSON 字节插入时间戳，access token 与 sign-version 头 |
| 课程与 LMS | 机构创建 / 编辑课程、课节、单元和活动等管理方法 | 当前用户的 member course_list 与已加入课程的目录 / 详情 |
| 云盘 | `get_folder_list`, `get_cloud_list`, `get_top_folder_id` 查机构云盘元数据 | 个人会话目录、引用上下文与原件刷新 |
| 回放页 | `get_webcast_url` 返回播放器页面，不是 MP4 原件 | 课堂回放入口与独立录播活动原件分别处理 |

官方 [v2 签名规则](https://docs.eeo.cn/api/zh-hans/appendix/signature.html) 与个人 JSON 签名不是同一算法。[getCloudList](https://docs.eeo.cn/api/zh-hans/cloud/getCloudList.html) 的文件响应仅列 `id`, `file_name`, `file_size`，并有“文件夹不属于该机构”错误；[getWebcastUrl](https://docs.eeo.cn/api/zh-hans/broadcast/getWebcastUrl.html) 返回 `webcast.php` 页面并检查课程所属机构。
