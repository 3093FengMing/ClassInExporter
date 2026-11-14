# ClassInExporter

中文 Python / PySide6 Qt 桌面资源导出工具。读取本机正在运行且已登录的 ClassIn 会话，按课程整理普通文件、视频资料和可用课堂回放，优先下载远程原件，必要时回退经确认完整的缓存。

## 安装与运行

### 使用 Windows 便携版

从本仓库的 Releases 下载 `ClassInExporter-0.4.0-win64.zip`，完整解压后运行 `ClassInExporter/ClassInExporter.exe`。保留旁边的 `_internal` 文件夹。无需安装 Python、FFmpeg 或 IDA；默认导出到 EXE 旁的 `exports`。

### 从源码运行

需要 64 位 Python 3.11 或更新版本。推荐为本项目创建独立环境：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

也可以双击 `run.bat`；它优先使用项目 `.venv`，再通过 Python Launcher 或 PATH 查找解释器。`.venv` 是每位开发者自行创建的依赖环境，不随源码分发，也不需要复制原作者的环境。

## 使用方法

1. 在 ClassIn 中登录并保持客户端运行，点击导出器的“获取资源”。默认自动检测；需要指定安装目录时打开“设置”。
2. 在课程目录选择课程、个人云盘、机构云盘或未归属资源，再用资源类型选项卡及名称搜索缩小范围。
3. 单击资源行查看信息；点击真实复选框或按空格勾选。双击或按回车打开可复制的完整名称与状态。
4. 底部持续显示全部已选和隐藏勾选数量。切换筛选保留勾选；“全选当前 / 取消当前 / 反选当前”只作用于当前可选列表。
5. 确认或更改导出位置，再点击“下载已选 N 项”。课程按“课程名 · 稳定短标识”分目录，重名课程保持独立。
6. 设置中获取线程与下载线程分别为 1–16，默认均为 4。后台并发执行，Qt 主线程更新界面；取消后停止启动剩余任务，并等待当前传输和临时文件清理。

已发现条目不等于已成功导出。文件下载会检查长度及适用摘要；标准 HLS 会下载片段与必要密钥并用 FFmpeg 合并。部分缓存、HTTP 206、变化中的文件或单独播放列表不能作为完整原件导出。已有同名文件保留，新结果自动增加序号。

## 分析结论与开发入口

当前实现结合公开前端请求定义、客户端导出符号与指令、实际响应以及下载样本验证。主要链路为：

```text
当前登录会话 → 课程 / 云盘索引 → 单元和活动详情
             → 统一资源模型 → 原件地址刷新 → 下载及完整性验证
```

- `classin_exporter/session.py`：当前会话读取、服务域名、签名、登录一致性与线程 transport。
- `classin_exporter/discovery.py`：课程、云盘、活动分页及资源聚合。
- `classin_exporter/lms_material.py`、`lms_record.py`、`lms_homework.py`：资料、录播与作业附件适配。
- `classin_exporter/resources.py`、`grouping.py`：统一资源结构、稳定标识及输出目录。
- `classin_exporter/downloader.py`、`cache.py`：远程原件、标准 HLS、Qt 缓存完整性及取消清理。
- `classin_exporter/gui.py`、`gui_workspace.py`、`gui_views.py`：后台调度与中文 Qt 课程目录界面。

详细的复现步骤、认证和签名规则、API 请求字段、响应结构、分页及验证边界见 [API.md](API.md)。它使用合成示例，不包含账号凭据、私人资源地址或本机绝对路径。

### 官方 SDK 是否适用

[官方 Python SDK](https://github.com/EeoApiSupport/classin-sdk-py) 和 [机构 API 文档](https://docs.eeo.cn/api/zh-hans/) 面向已开通 API 的机构，需要机构 SID 和密钥。它们与本项目的个人客户端登录会话不是同一认证来源。

本轮保持现有个人导出链路，不增加官方 SDK 强制依赖。未来具有机构授权时可以增加独立适配器；官方播放器页面不是 MP4 地址，录课文件推送还需要机构消息订阅与接收服务。详见 [API.md 中的官方接口比较](API.md#官方-sdk-与机构-api)。

## 已验证范围与限制

开发阶段已经验证普通远程 PDF、完整原始 MP4、完整缓存回退、Qt 批量下载和后台并发。一次真实 4 线程扫描耗时 11.64 秒，得到205条文件、45条视频及11个回放权限候选；两份 PDF 并发下载成功。资源数量仅表示发现的元数据。

- 当前个人账号的课堂回放候选返回809，录像存在性未确认，界面禁止勾选；未宣称取得可下载回放。
- 四条课程云盘补充目录请求返回HTTP404，可访问目录仍未确认，不等于空目录。已经取得的 LMS 资料和录播保留。
- 标准 HLS 合并通过本地视频夹具验证，尚未完成真实 ClassIn 回放 HLS 下载验证；专有 pm3u8 不作为标准 HLS。
- 原生会话读取针对已验证的 Windows x64 客户端结构；其他版本需要重新核验，无法确认时明确失败。

开发阶段最后一轮完整回归为181项通过。按照发布整理要求，测试目录、本地分析证据、设计过程文件和样本不随本仓库分发。程序的离线自检仍包含在构建入口中。

## 构建便携版

在 Windows x64 和 Python 3.11 环境中，从项目根运行：

```powershell
.\build\build.ps1
```

脚本自动创建本机 `.venv`、安装 `requirements-build.txt`、检查配置并执行离线自检，随后构建 EXE、运行冻结程序自检，生成 ZIP 和 SHA-256 文件。已有依赖时使用 `-SkipDependencyInstall`；只执行前置检查时使用 `-VerifyOnly`。

```powershell
.\build\build.ps1 -SkipDependencyInstall -VerifyOnly
.\build\build.ps1 -SkipDependencyInstall
```

输出位于 `dist/ClassInExporter/`。重新构建前，若此便携目录已经存在用户 `exports`，脚本会停止覆盖。自检不读取登录会话或请求账号资源。

## 许可证与版权

Copyright (c) 2026 **3093FengMing**。

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，属于源码可用的非商用许可。允许符合许可的非商用使用、修改和再发布；分发时必须保留完整许可条款或许可链接，以及 [NOTICE](NOTICE) 中的 Required Notice。许可范围之外的商业使用需要原作者另行授权。它不是 OSI 认可的开源许可证。

第三方依赖保留其各自版权与许可证；本项目许可不授予 ClassIn 服务、账号或课程资源的访问及内容版权。导出资源应符合本人已有的访问与使用权限。
