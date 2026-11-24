# 小米云查看器（miyun-viewer）

一个 Windows 上的**非官方**小米云桌面客户端：相册、笔记、待办、录音、云盘集中在一个窗口里，支持本地解密、缓存、多选批量操作。

技术栈：Python 3.12 + pywebview（WebView2）+ 单文件 HTML 界面。

> **非官方项目，与小米公司没有任何关联。** 只使用你自己的小米账号；请遵守小米的服务条款。代码按 MIT 协议“按现状”提供，不提供任何担保，账号风险、数据风险自负。

## 功能

- **相册**：全部照片时间轴、相机/截屏/自建相簿、视频、最近删除；缩略图虚拟滚动；大图预览（缩放/旋转/详情/GPS）；多选（Shift 区间）、批量下载、移动、复制、删除；回收站恢复/彻底删除。
- **笔记**：分类、阅读、所见即所得编辑、Markdown 表格/任务列表、笔记图片、本地解密。
- **待办**：全部/未完成/已完成、分页、新建/编辑/子项/勾选/删除；本地加密缓存 + 后台刷新。
- **录音**：播放（带进度）、下载、回收站恢复/彻底删除；加密录音本地解密/转码（需要 ffmpeg）。
- **云盘**：文件夹浏览、图片/视频预览、其他文件下载解密后交给系统打开；移动/复制/重命名/删除/上传、拖拽。
- **设置**：缓存明细、上限（自动 = 磁盘剩余的一半）、缓存目录迁移、账号信息、退出登录。
- **隐私**：登录票 `session.dat` 和待办缓存 `todos.dat` 使用 Windows DPAPI 加密；原始媒体缓存在本地 `cache/`；不上传任何数据。

## 免责声明与登录方式

程序支持两种登录：

1. **本程序自己的扫码 / 设备登录**（推荐，不碰官方客户端）。
2. **复用「小米云服务」官方客户端的登录态**：`client_auth.py` 会以只读方式扫描官方客户端进程内存，取出它当前的 cookie。

第 2 种方式**可能让官方客户端掉线、需要重新登录**：服务端每次响应都会轮换票据，反复读取会消耗官方客户端的登录态。代码保留该能力是历史原因，是否使用请自行评估；不想承担风险就只用第 1 种，并且不要运行官方客户端。

## 环境

- Windows 10/11（WebView2 Runtime，Win11 自带）
- Python 3.12
- 可选：ffmpeg（录音转码 / 视频探测）。放在程序目录、PATH，或设置 `MIYUN_FFMPEG` 指向 exe。
- 可选：代理。设置 `MIYUN_PROXY=http://127.0.0.1:7890`（按你的实际代理改）。

## 运行（源码）

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

首次启动按界面提示扫码或用设备登录。

只开本地 HTTP 服务、不弹窗口（调试界面用）：

```bat
python run_server.py
```

## appKey（加密照片 / 笔记）

小米的加密媒体需要 appKey 才能解密。换 appKey（需要官方客户端在运行，会用到 `client_auth`）：

```bat
python -I bind_appkey.py push
python -I bind_appkey.py 123456
```

换到的 appKey 存在 `cache/appkeys.json`（笔记的另存 `appkeys_note.json`），长期有效。**这两个文件是解密你数据的钥匙，绝对不要提交到 git、不要发给别人。**

## 构建 exe

```bat
pip install pyinstaller
pyinstaller miyun-viewer.spec
```

生成的 exe 在 `dist/`。spec 不会把 appKey 打进 exe；首次运行把 `cache/appkeys*.json` 放到 exe 旁边，或先运行 `bind_appkey.py`。

## 数据与隐私

- `session.dat`：登录票，DPAPI 加密，只在本机。
- `cache/todos.dat`：待办缓存，DPAPI 加密。
- `cache/`：缩略图/原图/录音/云盘缓存，可随时清空。
- 本仓库**不包含**任何人的登录票、appKey、照片、笔记或缓存。
- 公开仓库里的 `client_auth.py` 是完整的内存读取实现；再次提醒它可能影响官方客户端登录态。

## 已知限制

- 仅 Windows；依赖 WebView2。
- 小米接口随时可能变；待办在部分账号上需要重新验证（strongauth）。
- 加密媒体依赖 appKey；没有 appKey 只能看未加密内容。
- 内存 cookie 复用只适合个人自用；请优先用程序自己的登录。

## 许可

MIT，见 [LICENSE](LICENSE)。
