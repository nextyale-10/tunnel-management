param([long]$WindowHandle, [string]$OutputPath)
# Render only the test application's own window, never the full desktop.
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class TestWindowCapture {
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
}
'@
$bounds = New-Object TestWindowCapture+Rect
$window = [IntPtr]$WindowHandle
if (-not [TestWindowCapture]::GetWindowRect($window, [ref]$bounds)) { throw 'GetWindowRect failed' }
$bitmap = New-Object System.Drawing.Bitmap ($bounds.Right - $bounds.Left), ($bounds.Bottom - $bounds.Top)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$dc = $graphics.GetHdc()
try {
    if (-not [TestWindowCapture]::PrintWindow($window, $dc, 2)) { throw 'PrintWindow failed' }
} finally {
    $graphics.ReleaseHdc($dc)
    $graphics.Dispose()
}
try { $bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png) }
finally { $bitmap.Dispose() }
