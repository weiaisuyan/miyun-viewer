using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shell;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Miyun
{
    public class App : Application
    {
        [STAThread]
        public static void Main()
        {
            var app = new App();
            app.ShutdownMode = ShutdownMode.OnMainWindowClose;
            app.Run(new ShellWindow());
        }
    }

    /// <summary>
    /// 小米云 WPF 壳：WindowChrome 原生标题栏 + 系统 WebView2 装页面。
    /// 拖动 / 贴边吸附 / 双击最大化 / 四边拉伸 全部由 WindowChrome 与系统处理。
    /// </summary>
    public class ShellWindow : Window
    {
        const double BAR = 40;
        const string Brand = "小米云";
        const string DataDir = @"D:\程序\小米云";

        readonly WebView2 view = new WebView2();
        readonly TextBlock maxGlyph = new TextBlock();
        Process backend;
        int port;

        [DllImport("user32.dll")]
        static extern int GetWindowLong(IntPtr hWnd, int nIndex);
        [DllImport("user32.dll")]
        static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);

        [DllImport("dwmapi.dll")]
        static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);

        public ShellWindow()
        {
            port = FindFreePort();

            Title = Brand;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.CanResize;
            Background = new LinearGradientBrush(Color.FromRgb(0x12, 0x16, 0x29), Color.FromRgb(0x09, 0x0f, 0x17), 0);
            Width = 1280;
            Height = 860;
            MinWidth = 980;
            MinHeight = 620;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            LoadGeom();

            var chrome = new WindowChrome();
            chrome.CaptionHeight = BAR;
            chrome.ResizeBorderThickness = new Thickness(6);
            chrome.GlassFrameThickness = new Thickness(0);
            chrome.CornerRadius = new CornerRadius(0);
            chrome.UseAeroCaptionButtons = false;
            WindowChrome.SetWindowChrome(this, chrome);

            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(BAR) });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            var bar = BuildTitleBar();
            Grid.SetRow(bar, 0);
            root.Children.Add(bar);

            Grid.SetRow(view, 1);
            // 网页铺满整窗（不再留边），边框缩放靠给 WebView2 子窗口装 WM_NCHITTEST 钩子
            view.DefaultBackgroundColor = System.Drawing.Color.FromArgb(0x10, 0x14, 0x20);
            view.Margin = new Thickness(0);
            root.Children.Add(view);
            Content = root;

            SourceInitialized += OnSourceInitialized;
            Loaded += OnLoaded;
            StateChanged += delegate { UpdateMaxGlyph(); };
            rsTimer.Tick += delegate { rsTimer.Stop(); SetResizing(false); };
            Closing += delegate { SaveGeom(); KillBackend(); };
        }

        void OnSourceInitialized(object sender, EventArgs e)
        {
            try
            {
                IntPtr hwnd = new WindowInteropHelper(this).Handle;
                topHwnd = hwnd;
                int on = 1, round = 2, transitionsOff = 0;
                DwmSetWindowAttribute(hwnd, 20, ref on, 4);            // 深色
                DwmSetWindowAttribute(hwnd, 33, ref round, 4);         // 圆角
                DwmSetWindowAttribute(hwnd, 3, ref transitionsOff, 4); // 不禁止 DWM 过渡
                int ncOn = 2;                                          // DWMNCRP_ENABLED
                DwmSetWindowAttribute(hwnd, 2, ref ncOn, 4);

                // 无边框窗口默认没有最小化/最大化动画；补回 WS_THICKFRAME 让 DWM 认它是普通窗口。
                // WindowChrome 负责把内容铺满，加了也不会出现原生边框。
                // 关键：WindowStyle=None 的窗口没有 WS_CAPTION，DWM 就不放最小化/最大化动画。
                // WindowChrome 的 WM_NCCALCSIZE 已经把客户区铺满整窗，补回 WS_CAPTION
                // 不会画出系统标题栏，只是让系统动画生效（随记 EnsureCaptionStyle 同款做法）。
                int st = GetWindowLong(hwnd, -16);
                int st2 = st | 0x00C00000 | 0x00040000 | 0x00020000 | 0x00010000;
                if (st2 != st) SetWindowLong(hwnd, -16, st2);
                int ex = GetWindowLong(hwnd, -20);
                // WS_EX_TOOLWINDOW 会让窗口没有任务栏缩略图和系统动画，去掉它
                int ex2 = ex & ~0x00000080;
                if (ex2 != ex) SetWindowLong(hwnd, -20, ex2);
            }
            catch { }
            // 拖动窗口边框时：让页面先关掉毛玻璃和过渡，否则每帧重绘太慢，内容跟不上
            try
            {
                var src = PresentationSource.FromVisual(this) as HwndSource;
                if (src != null) src.AddHook(WndProc);
            }
            catch { }
        }

        IntPtr WndProc(IntPtr hwnd, int msg, IntPtr w, IntPtr l, ref bool handled)
        {
            if (msg == 0x0231) SetResizing(true);        // WM_ENTERSIZEMOVE
            else if (msg == 0x0232) SetResizing(false);  // WM_EXITSIZEMOVE
            else if (msg == 0x0005) { SetResizing(true); RestartRsTimer(); }  // WM_SIZE
            else if (msg == 0x0024) FixMaxSize(l);          // WM_GETMINMAXINFO
            return IntPtr.Zero;
        }

        readonly System.Windows.Threading.DispatcherTimer rsTimer = new System.Windows.Threading.DispatcherTimer();

        void RestartRsTimer()
        {
            rsTimer.Stop();
            rsTimer.Interval = TimeSpan.FromMilliseconds(280);
            rsTimer.Start();
        }

        void SetResizing(bool on)
        {
            try
            {
                if (view.CoreWebView2 == null) return;
                view.CoreWebView2.ExecuteScriptAsync(on
                    ? "document.body.classList.add('resizing')"
                    : "document.body.classList.remove('resizing')");
            }
            catch { }
        }

        FrameworkElement BuildTitleBar()
        {
            var bar = new Grid();
            bar.Background = new SolidColorBrush(Color.FromRgb(0x10, 0x14, 0x20));

            var left = new StackPanel();
            left.Orientation = Orientation.Horizontal;
            left.VerticalAlignment = VerticalAlignment.Center;
            left.Margin = new Thickness(16, 0, 0, 0);

            var mark = new Border();
            mark.Width = 20;
            mark.Height = 20;
            mark.CornerRadius = new CornerRadius(6);
            mark.Background = new LinearGradientBrush(
                Color.FromRgb(0x63, 0xB3, 0xFF), Color.FromRgb(0x0A, 0x84, 0xFF), 45);
            var cloud = new TextBlock();
            cloud.Text = "\u2601";
            cloud.FontSize = 12;
            cloud.Foreground = Brushes.White;
            cloud.HorizontalAlignment = HorizontalAlignment.Center;
            cloud.VerticalAlignment = VerticalAlignment.Center;
            mark.Child = cloud;
            left.Children.Add(mark);

            var name = new TextBlock();
            name.Text = Brand;
            name.FontSize = 12.5;
            name.Margin = new Thickness(9, 0, 0, 0);
            name.VerticalAlignment = VerticalAlignment.Center;
            name.Foreground = new SolidColorBrush(Color.FromRgb(0xE6, 0xEA, 0xF2));
            left.Children.Add(name);

            var right = new StackPanel();
            right.Orientation = Orientation.Horizontal;
            right.HorizontalAlignment = HorizontalAlignment.Right;

            var min = MakeButton("\uE921", false);
            min.Click += delegate { WindowState = WindowState.Minimized; };

            maxGlyph.Text = "\uE922";
            maxGlyph.FontFamily = new FontFamily("Segoe MDL2 Assets");
            maxGlyph.FontSize = 10;
            maxGlyph.Foreground = new SolidColorBrush(Color.FromRgb(0xD6, 0xDC, 0xE8));
            var max = MakeButton("", false);
            max.Content = maxGlyph;
            max.Click += delegate
            {
                WindowState = WindowState == WindowState.Maximized
                    ? WindowState.Normal : WindowState.Maximized;
            };

            var close = MakeButton("\uE8BB", true);
            close.Click += delegate { Close(); };

            right.Children.Add(min);
            right.Children.Add(max);
            right.Children.Add(close);

            bar.Children.Add(left);
            bar.Children.Add(right);
            return bar;
        }

        Button MakeButton(string glyph, bool isClose)
        {
            var b = new Button();
            b.Width = 46;
            b.Height = BAR;
            b.Padding = new Thickness(0);
            b.BorderThickness = new Thickness(0);
            b.Focusable = false;
            b.Background = Brushes.Transparent;
            b.Cursor = Cursors.Arrow;
            // WPF 默认按钮模板自带浅蓝悬停底色，必须换成纯色 Border 模板，否则会出亮青色
            var tpl = new ControlTemplate(typeof(Button));
            var fef = new FrameworkElementFactory(typeof(Border));
            fef.SetValue(Border.BackgroundProperty,
                new TemplateBindingExtension(Button.BackgroundProperty));
            fef.SetValue(Border.SnapsToDevicePixelsProperty, true);
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            cp.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
            fef.AppendChild(cp);
            tpl.VisualTree = fef;
            b.Template = tpl;

            var tb = new TextBlock();
            tb.Text = glyph;
            tb.FontFamily = new FontFamily("Segoe MDL2 Assets");
            tb.FontSize = 10;
            tb.Foreground = new SolidColorBrush(Color.FromRgb(0xD6, 0xDC, 0xE8));
            if (glyph.Length > 0) b.Content = tb;

            SolidColorBrush normal = Brushes.Transparent;
            SolidColorBrush hover = new SolidColorBrush(isClose
                ? Color.FromRgb(0xE8, 0x11, 0x23)
                : Color.FromRgb(0x1C, 0x23, 0x33));

            b.MouseEnter += delegate { b.Background = hover; };
            b.MouseLeave += delegate { b.Background = normal; };
            WindowChrome.SetIsHitTestVisibleInChrome(b, true);
            return b;
        }

        void UpdateMaxGlyph()
        {
            maxGlyph.Text = WindowState == WindowState.Maximized ? "\uE923" : "\uE922";
        }

        async void OnLoaded(object sender, RoutedEventArgs e)
        {
            // 父进程如果是隐藏窗口启动，Windows 会把首帧的显示状态继承成最小化
            if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
            Activate();
            try
            {
                StartBackend();
                await WaitServer(port, 90000);

                string dir = AppDomain.CurrentDomain.BaseDirectory;
                string ud = Path.Combine(dir, "_wv2");
                var env = await CoreWebView2Environment.CreateAsync(null, ud, null);
                await view.EnsureCoreWebView2Async(env);
                view.CoreWebView2.WebMessageReceived += OnWebMessage;
                view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                view.CoreWebView2.Settings.IsStatusBarEnabled = false;
                view.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = true;
                view.Source = new Uri("http://127.0.0.1:" + port + "/");

                // WebView2 的子窗口是懒创建的，隔一会儿多挂几次钩子，把后出现的也覆盖上
                var hookTimer = new System.Windows.Threading.DispatcherTimer();
                int n = 0;
                hookTimer.Interval = TimeSpan.FromMilliseconds(600);
                hookTimer.Tick += delegate
                {
    
                    if (++n > 30) hookTimer.Stop();
                };
                hookTimer.Start();


            }
            catch (Exception ex)
            {
                MessageBox.Show("启动失败：" + ex.Message, Brand);
            }
        }

        /* ---------- 边框缩放：给 WebView2 的子窗口装 WM_NCHITTEST ----------
           网页铺满整窗后，鼠标全落在子窗口上，父窗口收不到消息，所以拉伸要
           在子窗口这一层回答「这是边框」，Windows 就会走系统自带的缩放循环。 */
        delegate IntPtr WndProcDelegate(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
        delegate bool EnumChildProc(IntPtr hWnd, IntPtr lParam);

        [StructLayout(LayoutKind.Sequential)]
        struct RECT { public int Left, Top, Right, Bottom; }

        [DllImport("user32.dll", SetLastError = true)]
        static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);
        [DllImport("user32.dll")]
        static extern IntPtr CallWindowProc(IntPtr lpPrevWndFunc, IntPtr hWnd, int msg,
            IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")]
        static extern bool EnumChildWindows(IntPtr hWndParent, EnumChildProc cb, IntPtr lParam);
        [DllImport("user32.dll")]
        static extern bool GetWindowRect(IntPtr hWnd, out RECT r);

        const int GWL_WNDPROC = -4;
        const int WM_NCHITTEST = 0x0084;
        const int BAND = 6;

        IntPtr topHwnd;
        WndProcDelegate hitProc;
        readonly System.Collections.Generic.Dictionary<IntPtr, IntPtr> oldProcs =
            new System.Collections.Generic.Dictionary<IntPtr, IntPtr>();

        void HookWebViewChildren()
        {
            try
            {
                if (hitProc == null) hitProc = new WndProcDelegate(HitTestProc);
                IntPtr fp = Marshal.GetFunctionPointerForDelegate(hitProc);
                EnumChildWindows(topHwnd, delegate(IntPtr h, IntPtr l)
                {
                    if (oldProcs.ContainsKey(h)) return true;
                    IntPtr prev = SetWindowLongPtr(h, GWL_WNDPROC, fp);
                    if (prev != IntPtr.Zero) oldProcs[h] = prev;
                    return true;
                }, IntPtr.Zero);
            }
            catch { }
        }

        IntPtr HitTestProc(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam)
        {
            if (msg == WM_NCHITTEST && WindowState != WindowState.Maximized)
            {
                try
                {
                    int lp = lParam.ToInt32();
                    int sx = (short)(lp & 0xFFFF);
                    int sy = (short)((lp >> 16) & 0xFFFF);
                    RECT r;
                    GetWindowRect(topHwnd, out r);
                    bool left = sx < r.Left + BAND, right = sx >= r.Right - BAND;
                    bool top = sy < r.Top + BAND, bottom = sy >= r.Bottom - BAND;
                    if (top && left) return (IntPtr)13;      // HTTOPLEFT
                    if (top && right) return (IntPtr)14;     // HTTOPRIGHT
                    if (bottom && left) return (IntPtr)16;   // HTBOTTOMLEFT
                    if (bottom && right) return (IntPtr)17;  // HTBOTTOMRIGHT
                    if (left) return (IntPtr)10;             // HTLEFT
                    if (right) return (IntPtr)11;            // HTRIGHT
                    if (top) return (IntPtr)12;              // HTTOP
                    if (bottom) return (IntPtr)15;           // HTBOTTOM
                }
                catch { }
            }
            IntPtr prev;
            if (oldProcs.TryGetValue(hWnd, out prev) && prev != IntPtr.Zero)
                return CallWindowProc(prev, hWnd, msg, wParam, lParam);
            return IntPtr.Zero;
        }


        /* ---------- 网页端缩放热区 → 壳改窗口尺寸 ---------- */
        double rsL, rsT, rsW, rsH;

        void OnWebMessage(object sender, Microsoft.Web.WebView2.Core.CoreWebView2WebMessageReceivedEventArgs e)
        {
            try
            {
                string msg = e.TryGetWebMessageAsString();
                if (msg != null) msg = msg.Trim('"');   // 有些版本会带着 JSON 引号回来
                if (string.IsNullOrEmpty(msg)) return;
                string[] p = msg.Split('|');
                if (p[0] == "rzstart")
                {
                    rsL = Left; rsT = Top; rsW = Width; rsH = Height;
                }
                else if (p[0] == "rzmove" && p.Length >= 4)
                {
                    string d = p[1];
                    double dx = double.Parse(p[2], System.Globalization.CultureInfo.InvariantCulture);
                    double dy = double.Parse(p[3], System.Globalization.CultureInfo.InvariantCulture);
                    double l = rsL, t = rsT, w = rsW, h = rsH;
                    bool west = d.IndexOf('w') >= 0, east = d.IndexOf('e') >= 0;
                    bool north = d.IndexOf('n') >= 0, south = d.IndexOf('s') >= 0;
                    if (east) w = rsW + dx;
                    if (west) { w = rsW - dx; l = rsL + dx; }
                    if (south) h = rsH + dy;
                    if (north) { h = rsH - dy; t = rsT + dy; }
                    if (w < MinWidth) { if (west) l = rsL + (rsW - MinWidth); w = MinWidth; }
                    if (h < MinHeight) { if (north) t = rsT + (rsH - MinHeight); h = MinHeight; }
                    Left = l; Top = t; Width = w; Height = h;
                }
            }
            catch { }
        }

        /* ---------- 最大化时按工作区算，别被任务栏撑大 ---------- */
        [StructLayout(LayoutKind.Sequential)]
        struct POINT { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)]
        struct MINMAXINFO
        {
            public POINT ptReserved, ptMaxSize, ptMaxPosition, ptMinTrackSize, ptMaxTrackSize;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct MONITORINFO
        {
            public int cbSize; public RECT rcMonitor; public RECT rcWork; public int dwFlags;
        }
        [DllImport("user32.dll")]
        static extern IntPtr MonitorFromWindow(IntPtr hwnd, int flags);
        [DllImport("user32.dll")]
        static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO mi);

        void FixMaxSize(IntPtr lParam)
        {
            try
            {
                IntPtr mon = MonitorFromWindow(topHwnd, 2);      // MONITOR_DEFAULTTONEAREST
                var mi = new MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
                if (!GetMonitorInfo(mon, ref mi)) return;
                var mmi = (MINMAXINFO)Marshal.PtrToStructure(lParam, typeof(MINMAXINFO));
                mmi.ptMaxPosition.X = mi.rcWork.Left - mi.rcMonitor.Left;
                mmi.ptMaxPosition.Y = mi.rcWork.Top - mi.rcMonitor.Top;
                mmi.ptMaxSize.X = mi.rcWork.Right - mi.rcWork.Left;
                mmi.ptMaxSize.Y = mi.rcWork.Bottom - mi.rcWork.Top;
                Marshal.StructureToPtr(mmi, lParam, true);
            }
            catch { }
        }

        void Dbg(string text)
        {
            try { File.AppendAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "rz.log"), text + "\n"); }
            catch { }
        }


        void StartBackend()
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            string cmd = Path.Combine(dir, "backend.cmd");
            if (!File.Exists(cmd)) return;

            var psi = new ProcessStartInfo("cmd.exe",
                "/c \"\"" + cmd + "\" " + port + " \"" + DataDir + "\"\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WorkingDirectory = dir;
            backend = Process.Start(psi);
        }

        static async Task WaitServer(int port, int timeoutMs)
        {
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < timeoutMs)
            {
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(
                        "http://127.0.0.1:" + port + "/");
                    req.Timeout = 1500;
                    req.Proxy = null;
                    using (var resp = (HttpWebResponse)req.GetResponse())
                    {
                        if ((int)resp.StatusCode == 200) return;
                    }
                }
                catch { }
                await Task.Delay(200);
            }
            throw new Exception("后端 90 秒内没有就绪（端口 " + port + "）");
        }

        void KillBackend()
        {
            try
            {
                if (backend != null && !backend.HasExited)
                {
                    var psi = new ProcessStartInfo("taskkill",
                        "/PID " + backend.Id + " /T /F");
                    psi.CreateNoWindow = true;
                    psi.UseShellExecute = false;
                    Process.Start(psi);
                }
            }
            catch { }
        }

        static int FindFreePort()
        {
            var l = new TcpListener(IPAddress.Loopback, 0);
            l.Start();
            int p = ((IPEndPoint)l.LocalEndpoint).Port;
            l.Stop();
            return p;
        }

        string StateFile
        {
            get { return Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "shell.json"); }
        }

        void LoadGeom()
        {
            try
            {
                if (!File.Exists(StateFile)) return;
                string[] s = File.ReadAllText(StateFile).Split(',');
                if (s.Length < 5) return;
                double x, y, w, h;
                if (!double.TryParse(s[0], out x)) return;
                if (!double.TryParse(s[1], out y)) return;
                if (!double.TryParse(s[2], out w)) return;
                if (!double.TryParse(s[3], out h)) return;
                if (w < 400 || h < 300) return;
                WindowStartupLocation = WindowStartupLocation.Manual;
                Left = x; Top = y; Width = w; Height = h;
                if (s[4].Trim() == "1") WindowState = WindowState.Maximized;
            }
            catch { }
        }

        void SaveGeom()
        {
            try
            {
                Rect r = WindowState == WindowState.Normal
                    ? new Rect(Left, Top, Width, Height)
                    : RestoreBounds;
                File.WriteAllText(StateFile, string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "{0},{1},{2},{3},{4}",
                    (int)r.X, (int)r.Y, (int)r.Width, (int)r.Height,
                    WindowState == WindowState.Maximized ? 1 : 0));
            }
            catch { }
        }
    }
}
