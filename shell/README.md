# 壳（WPF + WebView2）

原来的界面跑在 pywebview 的窗口里，Windows 原生标题栏和界面永远是两块拼在一起的皮。
这里改成 WPF 壳：壳负责窗口（自绘标题栏、缩放、系统动画），网页只管内容，
后端（Python）当独立进程跑，壳启动时拉起、关闭时收掉。

## 组成

| 文件 | 作用 |
|---|---|
| `Shell.cs` | 全部窗口逻辑：标题栏、最小化/最大化/关闭、边框缩放、几何记忆、后端进程管理 |
| `build.cmd` | 用 .NET Framework 自带的 csc 直接编译成 `dist\MiyunShell.exe`（不需要装 SDK） |
| `backend.cmd` | 壳用来启动后端的命令行：`backend.cmd <端口> <数据目录>` |
| `lib\*.dll` | WebView2 的托管 DLL（`Microsoft.Web.WebView2.Core.dll`、`.Wpf.dll`、`WebView2Loader.dll`），编译和运行都要 |

`lib\` 不在仓库里（二进制）。取法：随记（TempNotes）的发布包目录里就有这三个 DLL，
或者装 WebView2 NuGet 包后从 `runtimes\win-x64\native` 与 `lib\net45` 取。

## 编译

```bat
build.cmd
```

产物：`dist\MiyunShell.exe`。把它和 `backend.cmd`、`app.ico`、`lib\*.dll` 放到一起，
`backend.cmd` 里指向 `app.py --headless --port %1 --data-dir %2` 即可。

## 几个坑（都踩过）

- **`WindowStyle=None` 没有 `WS_CAPTION`，DWM 不放最小化/最大化动画**。做法同随记：
  运行时把 `WS_CAPTION` 位补回去，`WindowChrome` 的 `WM_NCCALCSIZE` 保证客户区仍铺满整窗，系统标题栏画不出来。
- **WebView2 是独立子窗口，会把边框缩放的鼠标命中整个吃掉**。这里不靠边框留白（会露出一圈不同色的边），
  而是网页自己在四边四角放 5~10px 热区，按下后 `postMessage('rzmove|e|12|0')` 通知壳改窗口尺寸。
- **`postMessage` 的字符串可能带着 JSON 引号回来**，解析前先 `Trim('"')`，否则 `rzstart|e` 永远配不上。
- 最大化要自己按工作区算（`WM_GETMINMAXINFO`），否则窗口会被任务栏撑大、底部内容被切掉。
