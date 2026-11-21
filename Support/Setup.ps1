[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$SelfTest,
    [string]$CacheRoot,
    [string]$RenderPreview,
    [ValidatePattern('^\d{3,4}x\d{3,4}$')]
    [string]$RenderSize,
    [ValidateSet('trust','developer','apps','vpn','transfer','launcher','home','done','error')]
    [string]$PreviewStage
)

$ErrorActionPreference = 'Stop'
trap {
    $startupMessage = 'Location Map setup could not open. Keep the complete USB folder together, close any other setup window, and try START HERE again.'
    if ($Check -or $SelfTest -or $RenderPreview) {
        Write-Output $startupMessage
    } else {
        try { [System.Windows.Forms.MessageBox]::Show($startupMessage,'Location Map setup') | Out-Null }
        catch {
            try { $popup = New-Object -ComObject WScript.Shell; $popup.Popup($startupMessage,0,'Location Map setup',16) | Out-Null }
            catch { }
        }
    }
    exit 1
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# Worker threads touch neither PowerShell runspaces nor Windows controls.
Add-Type -ReferencedAssemblies @('System','System.Core','System.IO.Compression','System.IO.Compression.FileSystem') -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace LocationMapSetup {
    public sealed class Bootstrap {
        public Task Work;
        public string Python;
        public string Error;
        public static Bootstrap Start(string zip, string checksum, string cacheRoot) {
            var result = new Bootstrap();
            result.Work = Task.Run(() => {
                try { result.Python = Extract(zip, checksum, cacheRoot); }
                catch (Exception) { result.Error = "The setup files could not be opened. Keep the complete USB folder together, then try again."; }
            });
            return result;
        }
        static string Hex(byte[] value) {
            return BitConverter.ToString(value).Replace("-", "").ToLowerInvariant();
        }
        static bool ChildOf(string path, string root) {
            return Path.GetFullPath(path).StartsWith(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
        }
        static bool CacheMatchesArchive(string zip, string directory, string hash) {
            try {
                if (!Directory.Exists(directory) || (File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
                    return false;
                if (!File.Exists(Path.Combine(directory, "python.exe")) ||
                    File.ReadAllText(Path.Combine(directory, ".ready")).Trim() != hash) return false;
                int files = 0;
                using (var archive = ZipFile.OpenRead(zip)) {
                    foreach (var entry in archive.Entries) {
                        if (String.IsNullOrEmpty(entry.Name)) continue;
                        string path = Path.GetFullPath(Path.Combine(directory, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                        if (!ChildOf(path, directory) || !File.Exists(path)) return false;
                        var info = new FileInfo(path);
                        if (info.Length != entry.Length || (info.Attributes & FileAttributes.ReparsePoint) != 0) return false;
                        using (var sha = SHA256.Create())
                        using (var expected = entry.Open())
                        using (var actual = File.OpenRead(path))
                            if (Hex(sha.ComputeHash(expected)) != Hex(sha.ComputeHash(actual))) return false;
                        files++;
                    }
                }
                return Directory.GetFiles(directory, "*", SearchOption.AllDirectories).Length == files + 1;
            } catch (IOException) { return false; }
              catch (UnauthorizedAccessException) { return false; }
        }
        static string Extract(string zip, string checksum, string cacheRoot) {
            string hash = File.ReadAllText(checksum).Trim().ToLowerInvariant();
            if (hash.Length != 64) throw new InvalidDataException();
            foreach (char c in hash) if (!Uri.IsHexDigit(c)) throw new InvalidDataException();
            using (var source = File.OpenRead(zip))
            using (var sha = SHA256.Create())
                if (Hex(sha.ComputeHash(source)) != hash) throw new InvalidDataException();
            cacheRoot = Path.GetFullPath(cacheRoot);
            Directory.CreateDirectory(cacheRoot);
            // Keep native extension DLL paths below Windows loader limits.
            // The full digest still verifies the archive, marker, and every file.
            string folder = hash.Substring(0, 20);
            string final = Path.Combine(cacheRoot, folder);
            if (!ChildOf(final, cacheRoot)) throw new InvalidDataException();
            using (var mutex = new Mutex(false, "Local\\LocationMapRuntime-" + hash)) {
                bool held = false;
                try {
                    try { held = mutex.WaitOne(TimeSpan.FromSeconds(90)); }
                    catch (AbandonedMutexException) { held = true; }
                    if (!held) throw new IOException();
                    string marker = Path.Combine(final, ".ready");
                    string python = Path.Combine(final, "python.exe");
                    if (CacheMatchesArchive(zip, final, hash))
                        return python;
                    string staging = Path.Combine(cacheRoot, folder + ".partial-" + Guid.NewGuid().ToString("N"));
                    if (!ChildOf(staging, cacheRoot)) throw new InvalidDataException();
                    Directory.CreateDirectory(staging);
                    using (var archive = ZipFile.OpenRead(zip)) {
                        if (archive.Entries.Count == 0 || archive.Entries.Count > 50000) throw new InvalidDataException();
                        long total = 0;
                        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                        foreach (var entry in archive.Entries) {
                            string name = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                            if (String.IsNullOrEmpty(name) || Path.IsPathRooted(name) || name.Contains(":")) throw new InvalidDataException();
                            string output = Path.GetFullPath(Path.Combine(staging, name));
                            if (!ChildOf(output, staging) || !names.Add(output)) throw new InvalidDataException();
                            if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000) throw new InvalidDataException();
                            total = checked(total + entry.Length);
                            if (total > 1024L * 1024 * 1024) throw new InvalidDataException();
                        }
                        foreach (var entry in archive.Entries) {
                            string output = Path.GetFullPath(Path.Combine(staging, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                            if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(output); continue; }
                            Directory.CreateDirectory(Path.GetDirectoryName(output));
                            using (var source = entry.Open())
                            using (var target = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                                source.CopyTo(target);
                        }
                    }
                    if (!File.Exists(Path.Combine(staging, "python.exe"))) throw new InvalidDataException();
                    File.WriteAllText(Path.Combine(staging, ".ready"), hash, Encoding.ASCII);
                    // A failed older extraction is moved aside only within our cache.
                    // No user folder or other installed runtime is deleted.
                    if (Directory.Exists(final)) {
                        string old = Path.Combine(cacheRoot, folder + ".incomplete-" + Guid.NewGuid().ToString("N"));
                        if (!ChildOf(old, cacheRoot) || !ChildOf(final, cacheRoot)) throw new InvalidDataException();
                        Directory.Move(final, old);
                    }
                    Directory.Move(staging, final);
                    return Path.Combine(final, "python.exe");
                } finally { if (held) mutex.ReleaseMutex(); }
            }
        }
    }

    public sealed class Backend : IDisposable {
        public readonly ConcurrentQueue<string> Lines = new ConcurrentQueue<string>();
        Process process;
        public int ExitCode;
        public bool Complete;
        public Backend(string python, string script, bool check) {
            var info = new ProcessStartInfo(python);
            info.Arguments = "-I -B \"" + script + "\"" + (check ? " --check" : "");
            info.WorkingDirectory = Path.GetDirectoryName(script);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.RedirectStandardInput = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.StandardErrorEncoding = Encoding.UTF8;
            foreach (string key in new [] { "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT", "USBMUXD_SOCKET_ADDRESS" })
                info.EnvironmentVariables.Remove(key);
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            process = new Process();
            process.StartInfo = info;
            process.OutputDataReceived += (s, e) => { if (e.Data != null) Lines.Enqueue(e.Data); };
            // Upstream diagnostics can contain device details. Never put them in UI.
            process.ErrorDataReceived += (s, e) => { };
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        public bool Poll() {
            if (Complete) return true;
            if (process == null || !process.HasExited) return false;
            process.WaitForExit(); // Drain async output before the UI consumes completion.
            ExitCode = process.ExitCode;
            Complete = true;
            return true;
        }
        public void Dispose() {
            if (process == null) return;
            try { if (!process.HasExited) process.Kill(); } catch (InvalidOperationException) { }
            process.Dispose();
            process = null;
        }
    }
}
'@

$script:Kit = [IO.Path]::GetFullPath($PSScriptRoot)
$script:Backend = $null
$script:Bootstrap = $null
$script:Python = $null
$script:LastStage = ''
$script:Finished = $false
$script:Closing = $false
$script:CacheRoot = $CacheRoot

function Start-Runtime {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
        throw 'This setup needs a Windows PC with an Intel or AMD 64-bit processor.'
    }
    if ([string]::IsNullOrWhiteSpace($script:CacheRoot)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'Windows local storage is unavailable.' }
        $script:CacheRoot = Join-Path $env:LOCALAPPDATA 'LocationMap\Runtime'
    }
    [LocationMapSetup.Bootstrap]::Start(
        (Join-Path $script:Kit 'Runtime.zip'),
        (Join-Path $script:Kit 'runtime.sha256'),
        ([IO.Path]::GetFullPath($script:CacheRoot)))
}

function Invoke-LocalCheck {
    $boot = Start-Runtime
    $boot.Work.Wait()
    if ($boot.Error) { throw $boot.Error }
    $worker = [LocationMapSetup.Backend]::new($boot.Python, (Join-Path $script:Kit 'Installer\wizard_backend.py'), $true)
    try {
        while (!$worker.Poll()) { Start-Sleep -Milliseconds 100 }
        $line = ''
        $received = $false
        $confirmed = $false
        while ($worker.Lines.TryDequeue([ref]$line)) {
            $received = $true
            try { $record = $line | ConvertFrom-Json; if ($record.stage -eq 'done') { $confirmed = $true } } catch { }
            Write-Output $line
        }
        if (!$received) { throw 'The setup tools could not start. Keep the complete Support folder beside START HERE and try again.' }
        if ($worker.ExitCode -eq 0 -and !$confirmed) { throw 'The package check did not confirm success. Keep the complete USB kit together and try again.' }
        return $worker.ExitCode
    } finally { $worker.Dispose() }
}

if ($Check) {
    try { $code = @(Invoke-LocalCheck); $code | Select-Object -SkipLast 1 | Write-Output; exit [int]$code[-1] }
    catch { Write-Output ('Setup check failed: ' + $_.Exception.Message); exit 1 }
}

[Windows.Forms.Application]::EnableVisualStyles()

Add-Type -ReferencedAssemblies @('System','System.Core','System.Drawing','System.Windows.Forms') -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
using System.Runtime.InteropServices;

namespace LocationMapPresentation {
    public static class Shapes {
        public static GraphicsPath Round(RectangleF rect, float radius) {
            var p = new GraphicsPath();
            float d = Math.Min(radius * 2, Math.Min(rect.Width, rect.Height));
            if (d <= 0) { p.AddRectangle(rect); return p; }
            p.AddArc(rect.Left, rect.Top, d, d, 180, 90);
            p.AddArc(rect.Right-d, rect.Top, d, d, 270, 90);
            p.AddArc(rect.Right-d, rect.Bottom-d, d, d, 0, 90);
            p.AddArc(rect.Left, rect.Bottom-d, d, d, 90, 90);
            p.CloseFigure(); return p;
        }
    }
    public sealed class StudioForm : Form {
        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd,int attribute,ref int value,int size);
        public StudioForm() { DoubleBuffered=true; ResizeRedraw=true; }
        protected override void OnHandleCreated(EventArgs e) {
            base.OnHandleCreated(e);
            try {
                int dark=1, caption=0x1B0F07, text=0xF9ECE1;
                DwmSetWindowAttribute(Handle,20,ref dark,4);
                DwmSetWindowAttribute(Handle,35,ref caption,4);
                DwmSetWindowAttribute(Handle,36,ref text,4);
            } catch (DllNotFoundException) { } catch (EntryPointNotFoundException) { }
        }
        protected override void OnPaintBackground(PaintEventArgs e) {
            if(ClientSize.Width<1||ClientSize.Height<1)return;
            using(var bg=new LinearGradientBrush(ClientRectangle,Color.FromArgb(7,15,27),Color.FromArgb(18,20,44),35f))e.Graphics.FillRectangle(bg,ClientRectangle);
            using(var pen=new Pen(Color.FromArgb(11,91,204,226))) {
                for(int x=0;x<Width;x+=40)e.Graphics.DrawLine(pen,x,0,x,Height);
                for(int y=0;y<Height;y+=40)e.Graphics.DrawLine(pen,0,y,Width,y);
            }
            e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            using(var ellipse=new GraphicsPath()) {
                ellipse.AddEllipse(Width-480,-250,720,650);
                using(var glow=new PathGradientBrush(ellipse)) {
                    glow.CenterColor=Color.FromArgb(42,134,85,255);
                    glow.SurroundColors=new[]{Color.FromArgb(0,134,85,255)};
                    e.Graphics.FillPath(glow,ellipse);
                }
            }
            using(var ellipse=new GraphicsPath()) {
                ellipse.AddEllipse(-350,Height-460,680,650);
                using(var glow=new PathGradientBrush(ellipse)) {
                    glow.CenterColor=Color.FromArgb(26,15,210,215);
                    glow.SurroundColors=new[]{Color.FromArgb(0,15,210,215)};
                    e.Graphics.FillPath(glow,ellipse);
                }
            }
        }
    }
    public sealed class Card : Panel {
        public Color Fill = Color.FromArgb(16,28,45);
        public Color Border = Color.FromArgb(47,69,102);
        public bool Decorated = true;
        public Card() {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        }
        protected override void OnPaint(PaintEventArgs e) {
            base.OnPaint(e);
            if (!Decorated) return;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Shapes.Round(new RectangleF(.5f,.5f,Width-1f,Height-1f),12f*e.Graphics.DpiX/96f))
            using (var fill = new SolidBrush(Fill))
            using (var border = new Pen(Border)) {
                e.Graphics.FillPath(fill,path); e.Graphics.DrawPath(border,path);
            }
        }
    }
    public sealed class MapMark : Control {
        public MapMark() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor,true); }
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            float s = Math.Min(Width,Height)/40f;
            e.Graphics.ScaleTransform(s,s);
            using (var p = Shapes.Round(new RectangleF(0,0,40,40),11))
            using (var blue = new LinearGradientBrush(new RectangleF(0,0,40,40),Color.FromArgb(16,203,207),Color.FromArgb(120,75,233),45f)) e.Graphics.FillPath(blue,p);
            using (var white = new SolidBrush(Color.White)) {
                e.Graphics.FillPolygon(white,new [] {new PointF(9,12),new PointF(15.7f,9),new PointF(15.7f,27),new PointF(9,30)});
                e.Graphics.FillPolygon(white,new [] {new PointF(17.7f,9),new PointF(23.2f,12),new PointF(23.2f,30),new PointF(17.7f,27)});
                e.Graphics.FillPolygon(white,new [] {new PointF(25.2f,12),new PointF(31,9),new PointF(31,27),new PointF(25.2f,30)});
            }
        }
    }
    public sealed class ActionButton : Button {
        bool hover, down;
        public bool Primary;
        public ActionButton() {
            FlatStyle=FlatStyle.Flat; FlatAppearance.BorderSize=0; UseVisualStyleBackColor=false;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor,true);
            BackColor=Color.Transparent;
        }
        protected override void OnMouseEnter(EventArgs e) { hover=true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hover=false; down=false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { down=true; Invalidate(); base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { down=false; Invalidate(); base.OnMouseUp(e); }
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            Color fill=Primary ? Color.FromArgb(29,207,151) : Color.FromArgb(19,34,54);
            Color border=Primary ? fill : Color.FromArgb(61,110,144);
            Color text=Primary ? Color.FromArgb(5,27,24) : Color.FromArgb(174,231,246);
            if (hover) fill=Primary ? Color.FromArgb(72,231,181) : Color.FromArgb(27,53,79);
            if (down) fill=Primary ? Color.FromArgb(18,172,125) : Color.FromArgb(19,43,67);
            if (!Enabled) { fill=Color.FromArgb(24,35,50); border=Color.FromArgb(45,58,74); text=Color.FromArgb(124,144,165); }
            using(var path=Shapes.Round(new RectangleF(.5f,.5f,Width-1f,Height-1f),8f*e.Graphics.DpiX/96f))
            using(var brush=new SolidBrush(fill))
            using(var pen=new Pen(border)) { e.Graphics.FillPath(brush,path); e.Graphics.DrawPath(pen,path); }
            TextRenderer.DrawText(e.Graphics,Text,Font,ClientRectangle,text,TextFormatFlags.HorizontalCenter|TextFormatFlags.VerticalCenter|TextFormatFlags.SingleLine|TextFormatFlags.EndEllipsis);
            if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics,Rectangle.Inflate(ClientRectangle,-5,-5),text,fill);
        }
    }
    public sealed class QrImage : Control {
        public Image Picture;
        public QrImage(){SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer,true);TabStop=false;}
        protected override void OnPaint(PaintEventArgs e){
            e.Graphics.Clear(Color.White);
            if(Picture==null)return;
            int modules=Math.Max(1,Picture.Width/8);
            int side=modules*Math.Max(1,Math.Min(Width,Height)/modules);
            var target=new Rectangle((Width-side)/2,(Height-side)/2,side,side);
            e.Graphics.InterpolationMode=InterpolationMode.NearestNeighbor;
            e.Graphics.PixelOffsetMode=PixelOffsetMode.Half;
            e.Graphics.DrawImage(Picture,target,0,0,Picture.Width,Picture.Height,GraphicsUnit.Pixel);
        }
        protected override void Dispose(bool disposing){if(disposing && Picture!=null){Picture.Dispose();Picture=null;}base.Dispose(disposing);}
    }
}
'@

$script:Ink = [Drawing.Color]::FromArgb(225,236,249)
$script:Muted = [Drawing.Color]::FromArgb(166,187,209)
$script:Surface = [Drawing.Color]::FromArgb(9,17,31)
$script:PanelFill = [Drawing.Color]::FromArgb(16,28,45)
$script:Cyan = [Drawing.Color]::FromArgb(105,226,245)
$script:Violet = [Drawing.Color]::FromArgb(187,165,255)
$script:BodyFont = New-Object Drawing.Font('Segoe UI',14)
$script:HintFont = New-Object Drawing.Font('Segoe UI',13)
$script:TitleFont = New-Object Drawing.Font('Segoe UI Semibold',24)
$script:CompactTitleFont = New-Object Drawing.Font('Segoe UI Semibold',20)
$script:LayoutBusy = $false
$script:QrCards = @{}
$script:Form = New-Object LocationMapPresentation.StudioForm
$script:Form.Text = 'Location Map setup'
$script:Form.Size = New-Object Drawing.Size(920,760)
$script:Form.MinimumSize = New-Object Drawing.Size(780,650)
$script:Form.StartPosition = 'CenterScreen'
$script:Form.AutoScaleMode = 'Dpi'
$script:Form.AutoScaleDimensions = New-Object Drawing.SizeF(96,96)
$script:Form.BackColor = $script:Surface
$script:Form.Font = New-Object Drawing.Font('Segoe UI',11)
$script:Form.ShowIcon = $false

$table = New-Object Windows.Forms.TableLayoutPanel
$table.Dock = 'Fill'
$table.BackColor = [Drawing.Color]::Transparent
$table.ColumnCount = 1
$table.RowCount = 4
$table.Padding = New-Object Windows.Forms.Padding(32,24,32,22)
foreach ($height in @(52,82,-1,96)) {
    $row = New-Object Windows.Forms.RowStyle
    if($height -lt 0){$row.SizeType='Percent';$row.Height=100}
    else{$row.SizeType='Absolute';$row.Height=$height}
    $table.RowStyles.Add($row)|Out-Null
}
$script:Form.Controls.Add($table)

$head = New-Object Windows.Forms.Panel
$head.Dock = 'Fill'
$head.BackColor = [Drawing.Color]::Transparent
$head.Margin = New-Object Windows.Forms.Padding(0)
$mark = New-Object LocationMapPresentation.MapMark
$mark.Size = New-Object Drawing.Size(36,36)
$mark.TabStop = $false
$mark.BackColor = [Drawing.Color]::Transparent
$brand = New-Object Windows.Forms.Label
$brand.UseMnemonic = $false
$brand.Text = 'Location Map'
$brand.AutoSize = $true
$brand.Font = New-Object Drawing.Font('Segoe UI Semibold',16)
$brand.ForeColor = $script:Cyan
$brand.BackColor = [Drawing.Color]::Transparent
$brand.Location = New-Object Drawing.Point(48,4)
$head.Controls.AddRange(@($mark,$brand))
$table.Controls.Add($head,0,0)

$script:Title = New-Object Windows.Forms.Label
$script:Title.UseMnemonic = $false
$script:Title.Dock = 'Fill'
$script:Title.Margin = New-Object Windows.Forms.Padding(0)
$script:Title.TextAlign = 'MiddleLeft'
$script:Title.Font = $script:TitleFont
$script:Title.ForeColor = $script:Ink
$script:Title.BackColor = [Drawing.Color]::Transparent
$table.Controls.Add($script:Title,0,1)

$bodyPanel = New-Object Windows.Forms.Panel
$bodyPanel.Dock = 'Fill'
$bodyPanel.BackColor = [Drawing.Color]::Transparent
$bodyPanel.Margin = New-Object Windows.Forms.Padding(0)
$bodyPanel.AutoScroll = $true
$bodyPanel.TabStop = $false
$script:Content = New-Object Windows.Forms.Panel
$script:Content.Location = New-Object Drawing.Point(0,0)
$script:Content.BackColor = [Drawing.Color]::Transparent
$script:Content.TabStop = $false
$bodyPanel.Controls.Add($script:Content)
$script:MessageCard = New-Object LocationMapPresentation.Card
$script:MessageCard.BackColor = [Drawing.Color]::Transparent
$script:MessageCard.TabStop = $false
$script:Body = New-Object Windows.Forms.Label
$script:Body.UseMnemonic = $false
$script:Body.AutoSize = $true
$script:Body.Font = New-Object Drawing.Font('Segoe UI',14)
$script:Body.ForeColor = $script:Ink
$script:Body.BackColor = $script:PanelFill
$script:MessageCard.Controls.Add($script:Body)
$script:Content.Controls.Add($script:MessageCard)
$script:QrPanel = New-Object Windows.Forms.FlowLayoutPanel
$script:QrPanel.FlowDirection = 'LeftToRight'
$script:QrPanel.WrapContents = $false
$script:QrPanel.Margin = New-Object Windows.Forms.Padding(0)
$script:QrPanel.Padding = New-Object Windows.Forms.Padding(0)
$script:QrPanel.TabStop = $false
$script:Content.Controls.Add($script:QrPanel)
$script:Actions = New-Object Windows.Forms.FlowLayoutPanel
$script:Actions.FlowDirection = 'LeftToRight'
$script:Actions.WrapContents = $true
$script:Actions.Margin = New-Object Windows.Forms.Padding(0)
$script:Actions.Padding = New-Object Windows.Forms.Padding(0)
$script:Content.Controls.Add($script:Actions)
$table.Controls.Add($bodyPanel,0,2)

function Open-SetupLink($url) {
    try { Start-Process -FilePath $url | Out-Null }
    catch { [Windows.Forms.MessageBox]::Show('Open the App Store on your iPhone and search for the app shown here.','Location Map')|Out-Null }
}

foreach($item in @(
    @('a-shell.png','a-Shell','https://apps.apple.com/us/app/a-shell/id1473805438'),
    @('localdevvpn.png','LocalDevVPN','https://apps.apple.com/us/app/localdevvpn/id6755608044')
)) {
    $imagePath = Join-Path $script:Kit ('Images\'+$item[0])
    if(!(Test-Path -LiteralPath $imagePath -PathType Leaf)){continue}
    $card = New-Object LocationMapPresentation.Card
    $card.BackColor = [Drawing.Color]::Transparent
    $card.TabStop = $false
    $caption = New-Object Windows.Forms.Label
    $caption.UseMnemonic = $false
    $caption.Text = $item[1]
    $caption.AutoSize = $true
    $caption.BackColor = $script:PanelFill
    $caption.ForeColor = $script:Cyan
    $card.Border = [Drawing.Color]::FromArgb(42,119,145)
    if ($item[1] -eq 'LocalDevVPN') {
        $caption.ForeColor = $script:Violet
        $card.Border = [Drawing.Color]::FromArgb(105,81,163)
    }
    $caption.Font = New-Object Drawing.Font('Segoe UI Semibold',15)
    $image = New-Object LocationMapPresentation.QrImage
    $loadedImage = [Drawing.Image]::FromFile($imagePath)
    try { $image.Picture = [Drawing.Bitmap]::new($loadedImage) }
    finally { $loadedImage.Dispose() }
    $image.BackColor = [Drawing.Color]::White
    $link = New-Object LocationMapPresentation.ActionButton
    $link.Text = 'App Store page'
    $link.Font = New-Object Drawing.Font('Segoe UI',10)
    $link.Tag = $item[2]
    $link.Add_Click({ Open-SetupLink ([string]$this.Tag) })
    $card.Controls.AddRange(@($caption,$image,$link))
    $script:QrCards[$item[2]] = @{Card=$card;Caption=$caption;Image=$image;Link=$link}
}

$bottom = New-Object Windows.Forms.Panel
$bottom.Dock = 'Fill'
$bottom.BackColor = [Drawing.Color]::Transparent
$bottom.Margin = New-Object Windows.Forms.Padding(0,16,0,0)
$script:Progress = New-Object Windows.Forms.ProgressBar
$script:Progress.Style = 'Marquee'
$script:Cancel = New-Object LocationMapPresentation.ActionButton
$script:Cancel.Text = 'Cancel'
$script:Cancel.Font = New-Object Drawing.Font('Segoe UI Semibold',11)
$script:Cancel.TabIndex = 20
$script:Retry = New-Object LocationMapPresentation.ActionButton
$script:Retry.Text = 'Try again'
$script:Retry.Font = New-Object Drawing.Font('Segoe UI Semibold',11)
$script:Retry.Primary = $true
$script:Retry.Visible = $false
$script:Retry.TabIndex = 19
$bottom.Controls.AddRange(@($script:Progress,$script:Retry,$script:Cancel))
$table.Controls.Add($bottom,0,3)
$script:Form.CancelButton = $script:Cancel

function Ui-Unit([double]$value) {
    $dpi = 96
    if($script:Form.IsHandleCreated){$dpi=$script:Form.DeviceDpi}
    return [int][Math]::Round($value*$dpi/96)
}

function Update-PresentationLayout {
    if($script:LayoutBusy){return}
    $script:LayoutBusy=$true
    try {
        $table.Padding = New-Object Windows.Forms.Padding((Ui-Unit 32),(Ui-Unit 24),(Ui-Unit 32),(Ui-Unit 22))
        $compact=$script:Form.ClientSize.Height -lt (Ui-Unit 620)
        $table.RowStyles[0].Height=Ui-Unit 52
        $table.RowStyles[1].Height=Ui-Unit 82
        $head.Visible=-not $compact
        if($compact){
            $table.RowStyles[0].Height=0
            $table.RowStyles[1].Height=Ui-Unit 64
            $script:Title.Font=$script:CompactTitleFont
        } else {$script:Title.Font=$script:TitleFont}
        $table.RowStyles[3].Height=Ui-Unit 96
        $mark.Size=New-Object Drawing.Size((Ui-Unit 36),(Ui-Unit 36))
        $brand.Location=New-Object Drawing.Point((Ui-Unit 48),(Ui-Unit 4))
        for($pass=0;$pass -lt 2;$pass++) {
            $scroll=0
            if($bodyPanel.VerticalScroll.Visible){$scroll=[Windows.Forms.SystemInformation]::VerticalScrollBarWidth}
            $width=[Math]::Max((Ui-Unit 240),$bodyPanel.ClientSize.Width-$scroll-2)
            $isApps=$script:LastStage -eq 'apps' -and $script:QrPanel.Controls.Count -gt 0
            $inset=Ui-Unit 24
            if($isApps){$inset=0}
            $textWidth=[Math]::Max((Ui-Unit 200),$width-2*$inset)
            $script:Body.MinimumSize=New-Object Drawing.Size($textWidth,0)
            $script:Body.MaximumSize=New-Object Drawing.Size($textWidth,0)
            $script:Body.Location=New-Object Drawing.Point($inset,$inset)
            $cardHeight=$script:Body.PreferredHeight+2*$inset
            $script:MessageCard.Bounds=New-Object Drawing.Rectangle(0,0,$width,$cardHeight)
            $y=$cardHeight
            if($isApps){
                $y+=Ui-Unit 18
                $gap=Ui-Unit 16
                $cardWidth=[int][Math]::Floor(($width-$gap)/2)
                $cardHeight=Ui-Unit 286
                $qrSide=164; $qrTop=62; $linkTop=238
                if($compact){$cardHeight=Ui-Unit 200;$qrSide=128;$qrTop=52;$linkTop=192}
                $script:QrPanel.Bounds=New-Object Drawing.Rectangle(0,$y,$width,($cardHeight+2))
                foreach($card in $script:QrPanel.Controls){
                    $card.Size=New-Object Drawing.Size($cardWidth,$cardHeight)
                    $card.Margin=New-Object Windows.Forms.Padding(0,0,$gap,0)
                    $qr=$null
                    foreach($candidate in $script:QrCards.Values){if($candidate.Card -eq $card){$qr=$candidate;break}}
                    if($qr){
                        $qr.Link.Visible=-not $compact
                        $qr.Caption.Location=New-Object Drawing.Point((Ui-Unit 24),(Ui-Unit 20))
                        $qr.Image.Size=New-Object Drawing.Size((Ui-Unit $qrSide),(Ui-Unit $qrSide))
                        $qr.Image.Location=New-Object Drawing.Point(([int](($cardWidth-(Ui-Unit $qrSide))/2)),(Ui-Unit $qrTop))
                        $qr.Link.Size=New-Object Drawing.Size((Ui-Unit 142),(Ui-Unit 32))
                        $qr.Link.Location=New-Object Drawing.Point(([int](($cardWidth-(Ui-Unit 142))/2)),(Ui-Unit $linkTop))
                    }
                }
                $y+=$cardHeight+2
            }
            if($script:Actions.Controls.Count -gt 0){
                $y+=Ui-Unit 16
                $script:Actions.Bounds=New-Object Drawing.Rectangle(0,$y,$width,(Ui-Unit 48))
                $y+=Ui-Unit 48
            } else {$script:Actions.Height=0}
            $script:Content.Size=New-Object Drawing.Size($width,($y+2))
            $bodyPanel.AutoScrollMinSize=New-Object Drawing.Size(0,($y+2))
        }
        $script:Progress.Bounds=New-Object Drawing.Rectangle(0,0,$bottom.ClientSize.Width,(Ui-Unit 18))
        $buttonY=Ui-Unit 30
        $buttonW=Ui-Unit 122
        $script:Cancel.Bounds=New-Object Drawing.Rectangle(($bottom.ClientSize.Width-$buttonW),$buttonY,$buttonW,(Ui-Unit 40))
        $script:Retry.Bounds=New-Object Drawing.Rectangle(($bottom.ClientSize.Width-2*$buttonW-(Ui-Unit 10)),$buttonY,$buttonW,(Ui-Unit 40))
    } finally {$script:LayoutBusy=$false}
}

$bodyPanel.Add_SizeChanged({Update-PresentationLayout})
$bottom.Add_SizeChanged({Update-PresentationLayout})
function Fit-SetupWindow {
    if ($RenderPreview) { return }
    $area = [Windows.Forms.Screen]::FromControl($script:Form).WorkingArea
    $availableWidth = [Math]::Max(320,$area.Width-24)
    $availableHeight = [Math]::Max(360,$area.Height-24)
    $script:Form.MinimumSize = New-Object Drawing.Size(([Math]::Min((Ui-Unit 780),$availableWidth)),([Math]::Min((Ui-Unit 650),$availableHeight)))
    $script:Form.Size = New-Object Drawing.Size(([Math]::Min($script:Form.Width,$availableWidth)),([Math]::Min($script:Form.Height,$availableHeight)))
    $x = [Math]::Max($area.Left,[Math]::Min($script:Form.Left,$area.Right-$script:Form.Width))
    $y = [Math]::Max($area.Top,[Math]::Min($script:Form.Top,$area.Bottom-$script:Form.Height))
    $script:Form.Location = New-Object Drawing.Point($x,$y)
}
$script:Form.Add_Shown({Fit-SetupWindow;Update-PresentationLayout;$script:Form.ActiveControl=$script:Cancel;$bodyPanel.AutoScrollPosition=New-Object Drawing.Point(0,0)})
$script:Form.Add_DpiChanged({Fit-SetupWindow;Update-PresentationLayout})

function Set-SetupStep($record) {
    $stage=[string]$record.stage
    if($stage -notin @('prepare','trust','developer','apps','vpn','transfer','launcher','home','done','error')){return}
    $stageChanged=$stage -ne $script:LastStage
    $script:LastStage=$stage
    $script:Title.Text=[string]$record.title
    $script:Body.Text=([string]$record.message).Replace("`n",[Environment]::NewLine)
    $script:Body.Font=$script:BodyFont
    $script:Body.ForeColor=$script:Ink
    $script:Body.BackColor=$script:PanelFill
    $script:MessageCard.Decorated=$true
    $script:MessageCard.Border=[Drawing.Color]::FromArgb(47,69,102)
    if($stage -eq 'error'){$script:MessageCard.Border=[Drawing.Color]::FromArgb(182,133,65)}
    if($stage -eq 'developer'){$script:MessageCard.Border=[Drawing.Color]::FromArgb(109,86,171)}
    $script:QrPanel.Controls.Clear()
    $script:Actions.Controls.Clear()
    $index=0
    foreach($action in @($record.actions)){
        if(!$action -or !$action.url){continue}
        $url=[string]$action.url
        if($url -notmatch '^https://(apps\.apple\.com|support\.apple\.com)/' -and $url -notmatch '^ms-windows-store://pdp/\?'){continue}
        if($stage -eq 'apps' -and $script:QrCards.ContainsKey($url)){
            $qr=$script:QrCards[$url]
            if(!$script:QrPanel.Controls.Contains($qr.Card)){$script:QrPanel.Controls.Add($qr.Card)}
            $qr.Link.TabIndex=$index
            $index++
            continue
        }
        $button=New-Object LocationMapPresentation.ActionButton
        $button.Text=[string]$action.label
        $button.Tag=$url
        $button.Font=New-Object Drawing.Font('Segoe UI Semibold',11)
        $button.Size=New-Object Drawing.Size((Ui-Unit 210),(Ui-Unit 40))
        $button.Margin=New-Object Windows.Forms.Padding(0,0,(Ui-Unit 10),0)
        $button.TabIndex=$index
        $index++
        $button.Add_Click({Open-SetupLink ([string]$this.Tag)})
        $script:Actions.Controls.Add($button)
    }
    $script:QrPanel.Visible=$stage -eq 'apps' -and $script:QrPanel.Controls.Count -gt 0
    $script:Actions.Visible=$script:Actions.Controls.Count -gt 0
    if($stage -eq 'apps' -and $script:QrPanel.Controls.Count -gt 0){
        $script:Title.Text='Install the remaining app'
        $script:Body.Text="Scan with your iPhone camera, then tap Get.`r`nApprove the download. Setup continues when it finishes."
        if($script:QrPanel.Controls.Count -gt 1){
            $script:Title.Text='Install the two apps'
            $script:Body.Text="Scan with your iPhone camera, then tap Get.`r`nApprove the downloads. Setup continues when both finish."
        }
        $script:Body.Font=$script:HintFont
        $script:Body.ForeColor=$script:Muted
        $script:Body.BackColor=[Drawing.Color]::Transparent
        $script:MessageCard.Decorated=$false
    }
    if($stage -eq 'prepare'){$script:Body.Text="Preparing your setup.`r`nKeep your iPhone connected and unlocked."}
    if($stage -eq 'developer'){
        $script:Body.Text="On your iPhone:`r`n`r`nSettings > Privacy & Security > Developer Mode`r`n`r`nTurn it on and tap Restart.`r`nAfter restarting, unlock the phone and tap Turn On.`r`nEnter its passcode if asked. Keep the cable connected."
    }
    if($null -ne $record.progress){
        $script:Progress.Style='Continuous'
        $script:Progress.Value=[Math]::Max(0,[Math]::Min(100,[int]$record.progress))
    } else {$script:Progress.Style='Marquee'}
    $script:Retry.Visible=$stage -eq 'error'
    $script:Cancel.Primary=$stage -eq 'done'
    if($stage -eq 'done'){$script:Finished=$true;$script:Cancel.Text='Close'}
    elseif($stage -eq 'error'){$script:Cancel.Text='Close'}
    else{$script:Cancel.Text='Cancel'}
    if($stage -eq 'error'){$script:Form.AcceptButton=$script:Retry}
    elseif($stage -eq 'done'){$script:Form.AcceptButton=$script:Cancel}
    else{$script:Form.AcceptButton=$null}
    if($stageChanged){
        if($script:Form.IsHandleCreated){
            if($stage -eq 'error'){$script:Form.ActiveControl=$script:Retry}else{$script:Form.ActiveControl=$script:Cancel}
        }
        $bodyPanel.AutoScrollPosition=New-Object Drawing.Point(0,0)
    }
    $script:MessageCard.Invalidate()
    $script:Cancel.Invalidate()
    Update-PresentationLayout
}

function Start-SetupBackend {
    if ($script:Backend) { $script:Backend.Dispose(); $script:Backend = $null }
    $script:Finished = $false
    $script:Retry.Visible = $false
    Set-SetupStep @{stage='prepare';title='Getting everything ready';message="Preparing the tools included on your USB.`n`nYou do not need to install Python or type any commands.";progress=2}
    if ($script:Python) {
        $script:Backend = [LocationMapSetup.Backend]::new($script:Python,(Join-Path $script:Kit 'Installer\wizard_backend.py'),$false)
    } else { $script:Bootstrap = Start-Runtime }
}

$previews = @{
    trust=@{stage='trust';title='Connect and unlock your iPhone';message="1. Connect your iPhone to this computer with its cable.`n2. Unlock the iPhone.`n3. If it asks, tap Trust and enter the passcode on the phone.`n`nThis window will continue automatically.";progress=8;actions=@(@{label='Open Apple Devices';url='ms-windows-store://pdp/?productid=9NP83LWLPZ9K'})}
    developer=@{stage='developer';title='Turn on Developer Mode';message="On your iPhone:`n`nSettings > Privacy & Security > Developer Mode`n`nTurn it on, restart, then unlock the phone and tap Turn On.`nKeep the cable connected. Setup will wait for you.";progress=18}
    apps=@{stage='apps';title='Install two free helper apps';message="Scan each code with your iPhone camera, then tap Get in the App Store.`n`nApprove Apple's download prompts. Setup continues when both apps finish.";progress=28;actions=@(@{label='a-Shell app page';url='https://apps.apple.com/us/app/a-shell/id1473805438'},@{label='LocalDevVPN app page';url='https://apps.apple.com/us/app/localdevvpn/id6755608044'})}
    vpn=@{stage='vpn';title='Allow the local VPN';message="On your iPhone, open LocalDevVPN and tap Connect.`n`nTap Allow if asked, then enter the iPhone passcode on the phone.`nThis VPN makes the local connection to your own phone.";progress=40}
    transfer=@{stage='transfer';title='Installing Location Map';message="Keep your phone connected and unlocked.`n`nWe are copying and checking the app files. No need to touch anything.";progress=62}
    launcher=@{stage='launcher';title='Open Location Map on the iPhone';message="1. Open LocalDevVPN, tap Connect, and approve its VPN prompt.`n`n2. Open Files > Browse > On My iPhone > a-Shell > Location Map.shortcut. Tap Add Shortcut. If you see a preview, use Share > Shortcuts.`n`n3. Run Location Map. Choose Allow for its VPN and location prompts, and Always Allow for sharing location with a-Shell. Setup is waiting for the map to connect.";progress=80}
    home=@{stage='home';title='Put Location Map on your Home Screen';message="In Shortcuts, open Location Map's menu and tap Add to Home Screen, then Add.`n`nDrag the Location Map icon to your first Home Screen page.`n`nKeep both helper apps installed. To hide a helper icon, hold it and choose Remove App > Remove from Home Screen.";progress=95}
    done=@{stage='done';title='Location Map is ready';message="Open Location Map from your Home Screen.`n`nTap the map to choose a place, then tap Simulate.`nTap Stop to return to your real location.`n`nKeep a-Shell and LocalDevVPN installed.";progress=100}
    error=@{stage='error';title='Setup needs a little attention';message="Unlock and reconnect the same iPhone, then choose Try again.`n`nYour phone's existing files are kept. Setup resumes the checked files instead of starting from scratch.";progress=45}
}

if ($SelfTest) {
    try {
        foreach ($name in $previews.Keys) {
            Set-SetupStep $previews[$name]
            if (!$script:Title.Text -or !$script:Body.Text -or $script:LastStage -ne $name) { throw 'A setup screen failed to render.' }
        }
        Set-SetupStep $previews.apps
        if ($script:QrPanel.Controls.Count -ne 2) { throw 'The two app codes are not available.' }
        Set-SetupStep @{stage='apps';title='Remaining app';message='Install the remaining app.';progress=30;actions=@(@{label='LocalDevVPN';url='https://apps.apple.com/us/app/localdevvpn/id6755608044'})}
        if ($script:QrPanel.Controls.Count -ne 1 -or $script:QrPanel.Controls[0] -ne $script:QrCards['https://apps.apple.com/us/app/localdevvpn/id6755608044'].Card) { throw 'An already installed app is still shown.' }
        Set-SetupStep @{stage='apps';title='App links';message='Find the named app in the App Store.';progress=30;actions=@(@{label='Invalid';url='https://unrelated.invalid/'})}
        if ($script:QrPanel.Controls.Count -ne 0 -or $script:Actions.Controls.Count -ne 0) { throw 'An unrecognized link was offered.' }
        $script:Form.Dispose()
        $result = @(Invoke-LocalCheck)
        $result | Select-Object -SkipLast 1 | Write-Output
        if ([int]$result[-1] -ne 0) { exit [int]$result[-1] }
        Write-Output 'Wizard screen and local runtime checks passed. No phone was opened.'
        exit 0
    } catch { Write-Output ('Wizard self-test failed: ' + $_.Exception.Message); exit 1 }
}

$script:Timer = New-Object Windows.Forms.Timer
$script:Timer.Interval = 150
$script:Timer.Add_Tick({
    try {
        if ($script:Bootstrap -and $script:Bootstrap.Work.IsCompleted) {
            $boot = $script:Bootstrap
            $script:Bootstrap = $null
            if ($boot.Error) { throw $boot.Error }
            $script:Python = $boot.Python
            $script:Backend = [LocationMapSetup.Backend]::new($script:Python,(Join-Path $script:Kit 'Installer\wizard_backend.py'),$false)
        }
        if ($script:Backend) {
            $complete = $script:Backend.Poll()
            $line = ''
            while ($script:Backend.Lines.TryDequeue([ref]$line)) {
                try {
                    $record = $line | ConvertFrom-Json
                    if ($record.stage -and $record.title -and $record.message) { Set-SetupStep $record }
                } catch { } # Never show unstructured upstream diagnostic output.
            }
            if ($complete) {
                $exitCode = $script:Backend.ExitCode
                $script:Backend.Dispose()
                $script:Backend = $null
                if ($exitCode -ne 0 -and $script:LastStage -ne 'error') {
                    Set-SetupStep @{stage='error';title='Setup could not finish';message="Keep the same iPhone connected and unlocked, then choose Try again.`n`nIf the iPhone asks for permission, approve it on the phone.";progress=0}
                } elseif ($exitCode -eq 0 -and $script:LastStage -ne 'done') {
                    Set-SetupStep @{stage='error';title='The final check is still needed';message="The installer did not confirm the final step. Choose Try again to continue with the same iPhone.";progress=90}
                }
            }
        }
    } catch {
        if ($script:Backend) { $script:Backend.Dispose(); $script:Backend = $null }
        Set-SetupStep @{stage='error';title='Setup needs attention';message="Keep the complete USB folder together. Close any other Location Map setup window, then choose Try again.";progress=0}
    }
})

$script:Retry.Add_Click({ if(!$PreviewStage){try { Start-SetupBackend } catch { Set-SetupStep $previews.error }} })
$script:Cancel.Add_Click({ $script:Form.Close() })
$script:Form.Add_FormClosing({
    $script:Closing = $true
    $script:Timer.Stop()
    if ($script:Backend) { $script:Backend.Dispose(); $script:Backend = $null }
})

if ($RenderPreview) {
    if (!$PreviewStage -or ![IO.Path]::IsPathRooted($RenderPreview) -or [IO.Path]::GetExtension($RenderPreview) -ne '.png') {
        throw 'RenderPreview requires PreviewStage and an absolute PNG output path.'
    }
    # Render only this authored form. No desktop capture, window manipulation,
    # phone access, runtime extraction, or backend process is used in this mode.
    Set-SetupStep $previews[$PreviewStage]
    $script:Retry.Enabled = $true
    $script:Cancel.Text = 'Close'
    if($RenderSize){
        $parts=$RenderSize.Split('x')
        $width=[int]$parts[0]; $height=[int]$parts[1]
        if($width -lt 620 -or $height -lt 480 -or $width -gt 2000 -or $height -gt 1800){throw 'RenderSize is outside the supported preview range.'}
        $script:Form.MinimumSize=New-Object Drawing.Size(620,480)
        $script:Form.Size=New-Object Drawing.Size($width,$height)
    }
    $target = [IO.Path]::GetFullPath($RenderPreview)
    if (!(Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($target)) -PathType Container)) {
        throw 'The preview output folder does not exist.'
    }
    $bitmap = $null
    try {
        $script:Form.Show()
        [Windows.Forms.Application]::DoEvents()
        $script:Form.PerformLayout()
        $table.PerformLayout()
        $bodyPanel.PerformLayout()
        $script:QrPanel.PerformLayout()
        Update-PresentationLayout
        [Windows.Forms.Application]::DoEvents()
        $bitmap = New-Object Drawing.Bitmap($script:Form.Width,$script:Form.Height)
        $bounds = New-Object Drawing.Rectangle(0,0,$script:Form.Width,$script:Form.Height)
        $script:Form.DrawToBitmap($bitmap,$bounds)
        $bitmap.Save($target,[Drawing.Imaging.ImageFormat]::Png)
        Write-Output ('Rendered preview: ' + $target)
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        $script:Form.Close()
        $script:Form.Dispose()
        $script:Timer.Dispose()
    }
    exit 0
}

$single = New-Object Threading.Mutex($false,'Local\LocationMapUsbSetup')
$held = $false
try {
    try { $held = $single.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (!$held) {
        [Windows.Forms.MessageBox]::Show('Location Map setup is already open. Please use that window.','Location Map') | Out-Null
        exit 0
    }
    if ($PreviewStage) {
        Set-SetupStep $previews[$PreviewStage]
        $script:Retry.Enabled = $true
        $script:Cancel.Text = 'Close'
    }
    else {
        $script:Form.Add_Shown({
            try { Start-SetupBackend; $script:Timer.Start() }
            catch { Set-SetupStep $previews.error }
        })
    }
    [Windows.Forms.Application]::Run($script:Form)
} finally {
    if ($script:Backend) { $script:Backend.Dispose() }
    $script:Timer.Dispose()
    $script:Form.Dispose()
    if ($held) { $single.ReleaseMutex() }
    $single.Dispose()
}
