using System;
using System.IO;
using System.Threading.Tasks;
using Microsoft.Maui.Storage;

#if ANDROID
using Microsoft.Maui.ApplicationModel;
#endif

namespace Ipv6Transfer.Mobile.Services;

/// <summary>
/// 跨平台存储位置辅助。纯静态类，无静态构造函数、无字段初始化副作用，
/// 所有文件系统与平台 API 访问均包裹 try/catch，任何环境下调用都不会抛异常。
/// <para>
/// Android：公共 Download 目录可写时使用 <c>/storage/emulated/0/Download/IPv6Transfer/&lt;sub&gt;</c>，
/// 否则回退到 App 专属外部目录（再不行回退到应用私有目录）。
/// Windows / macOS / iOS：<c>FileSystem.AppDataDirectory/IPv6Transfer/&lt;sub&gt;</c>。
/// </para>
/// </summary>
public static class MobileStorage
{
    private const string RootFolderName = "IPv6Transfer";

    /// <summary>Android 公共下载目录（Android 11+ 写它需要「所有文件访问权限」）。</summary>
    private const string AndroidPublicDownload = "/storage/emulated/0/Download";

    /// <summary>用于探测目录是否真的可写的临时目录/文件名。</summary>
    private const string ProbeName = ".ipv6transfer_write_probe";

    // ------------------------------------------------------------------
    // 公开 API
    // ------------------------------------------------------------------

    /// <summary>
    /// 返回并确保存在一个可写的默认目录：<c>base/IPv6Transfer/&lt;sub&gt;</c>。
    /// 创建失败时不抛异常，仍返回目标路径（用 <see cref="TryEnsureDirectory"/> 可获得错误原因）。
    /// </summary>
    public static string DefaultDirectory(string sub)
    {
        string path;

        try
        {
            string baseDirectory = GetBaseDirectory();
            string folder = CleanSubPath(sub);

            path = string.IsNullOrEmpty(folder)
                ? Path.Combine(baseDirectory, RootFolderName)
                : Path.Combine(baseDirectory, RootFolderName, folder);
        }
        catch
        {
            // 极端情况下 Path.Combine 失败：退回到应用私有目录，保证有返回值。
            try
            {
                path = Path.Combine(SafeAppDataDirectory(), RootFolderName);
            }
            catch
            {
                path = RootFolderName;
            }
        }

        TryEnsureDirectory(path, out _);
        return path;
    }

    /// <summary>
    /// 尝试创建目录（父目录会一并创建）。失败时返回 <c>false</c> 并给出中文错误信息。
    /// </summary>
    public static bool TryEnsureDirectory(string path, out string error)
    {
        error = string.Empty;

        if (string.IsNullOrWhiteSpace(path))
        {
            error = "目录路径为空，无法创建。";
            return false;
        }

        try
        {
            if (Directory.Exists(path))
            {
                return true;
            }

            Directory.CreateDirectory(path);

            if (Directory.Exists(path))
            {
                return true;
            }

            error = "创建目录失败：系统未返回错误信息。";
            return false;
        }
        catch (UnauthorizedAccessException ex)
        {
            error = "没有写入权限，无法创建目录：" + ex.Message;
            return false;
        }
        catch (IOException ex)
        {
            error = "创建目录时发生 I/O 错误：" + ex.Message;
            return false;
        }
        catch (ArgumentException ex)
        {
            error = "目录路径格式无效：" + ex.Message;
            return false;
        }
        catch (NotSupportedException ex)
        {
            error = "目录路径格式不受支持：" + ex.Message;
            return false;
        }
        catch (Exception ex)
        {
            error = "创建目录失败：" + ex.Message;
            return false;
        }
    }

    /// <summary>
    /// 当前是否已获得公共存储（Android 的 All files access，或 API &lt; 30 的旧版存储权限）访问权。
    /// 非 Android 平台恒为 <c>true</c>。
    /// </summary>
    public static bool HasPublicStorageAccess
    {
        get
        {
#if ANDROID
            try
            {
                return HasAndroidStoragePermission() && IsAndroidPublicDownloadWritable();
            }
            catch
            {
                return false;
            }
#else
            return true;
#endif
        }
    }

    /// <summary>
    /// Android：若无公共存储权限则跳转系统「所有文件访问权限」设置页（API 30+）
    /// 或请求旧版存储写权限（API &lt; 30），随后返回当前是否已授权。非 Android 直接返回 <c>true</c>。
    /// </summary>
    public static Task<bool> RequestPublicStorageAccessAsync()
    {
#if ANDROID
        return RequestAndroidPublicStorageAccessAsync();
#else
        return Task.FromResult(true);
#endif
    }

    /// <summary>
    /// 展示用根路径。Android 在公共 Download 可写时返回 <c>/storage/emulated/0/Download</c>，
    /// 否则返回 App 专属目录；其余平台返回 <c>FileSystem.AppDataDirectory</c>。
    /// </summary>
    public static string DisplayRoot
    {
        get
        {
            try
            {
                return GetBaseDirectory();
            }
            catch
            {
                return SafeAppDataDirectory();
            }
        }
    }

    /// <summary>
    /// 把一个绝对路径转成人类可读的简短说明（例如「公共 Download 目录 / IPv6Transfer/abc」）。
    /// 不会返回 null，也不会抛异常。
    /// </summary>
    public static string Describe(string path)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return "（空路径）";
            }

            string full = path.Trim()
                              .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                              .Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar);

            string described = DescribeAgainst(full, AndroidPublicDownload, "公共 Download 目录");
            if (described.Length > 0)
            {
                return described;
            }

            described = DescribeAgainst(full, GetBaseDirectory(), "默认存储目录");
            if (described.Length > 0)
            {
                return described;
            }

            described = DescribeAgainst(full, SafeAppDataDirectory(), "应用私有数据目录");
            if (described.Length > 0)
            {
                return described;
            }

            return "其他位置：" + full;
        }
        catch
        {
            return path is null ? "（空路径）" : path;
        }
    }

    // ------------------------------------------------------------------
    // 内部实现：公共部分
    // ------------------------------------------------------------------

    private static string GetBaseDirectory()
    {
#if ANDROID
        try
        {
            // 优先公共下载目录（可写时），否则 App 专属外部目录。
            if (IsAndroidPublicDownloadWritable() && Directory.Exists(AndroidPublicDownload))
            {
                return AndroidPublicDownload;
            }
        }
        catch
        {
            // 忽略，走 App 专属目录
        }

        return AndroidExternalAppDirectory();
#else
        return SafeAppDataDirectory();
#endif
    }

    /// <summary>去掉 <paramref name="sub"/> 两端的空白与目录分隔符，防止跳出根目录。</summary>
    private static string CleanSubPath(string? sub)
    {
        if (string.IsNullOrWhiteSpace(sub))
        {
            return string.Empty;
        }

        string cleaned = sub.Trim();

        cleaned = cleaned.Trim(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                         .Trim(' ', Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

        return cleaned;
    }

    private static string SafeAppDataDirectory()
    {
        try
        {
            string directory = FileSystem.AppDataDirectory;
            if (!string.IsNullOrWhiteSpace(directory))
            {
                return directory;
            }
        }
        catch
        {
            // 忽略，使用下面的兜底
        }

        try
        {
            return Path.GetTempPath();
        }
        catch
        {
            return RootFolderName;
        }
    }

    /// <summary>路径 <paramref name="full"/> 是否在 <paramref name="root"/> 之下；是则输出相对部分。</summary>
    private static bool TryGetRelative(string full, string root, out string relative)
    {
        relative = string.Empty;

        if (string.IsNullOrWhiteSpace(root))
        {
            return false;
        }

        string normalizedRoot = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                                    .Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar);

        if (string.Equals(full, normalizedRoot, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        if (full.StartsWith(normalizedRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            relative = full.Substring(normalizedRoot.Length + 1);
            return true;
        }

        return false;
    }

    private static string DescribeAgainst(string full, string root, string label)
    {
        if (!TryGetRelative(full, root, out string relative))
        {
            return string.Empty;
        }

        return string.IsNullOrEmpty(relative)
            ? label
            : label + " / " + relative.Replace(Path.DirectorySeparatorChar, '/');
    }

    // ------------------------------------------------------------------
    // 内部实现：Android
    // ------------------------------------------------------------------

#if ANDROID
    /// <summary>
    /// All files access（API 30+）或旧版写外部存储权限（API &lt; 30）是否已授予。
    /// </summary>
    private static bool HasAndroidStoragePermission()
    {
        try
        {
            if (OperatingSystem.IsAndroidVersionAtLeast(30))
            {
                return Android.OS.Environment.IsExternalStorageManager;
            }

            // API 21/22：外部存储写权限是安装时授予的，无需运行时检查。
            if (!OperatingSystem.IsAndroidVersionAtLeast(23))
            {
                return true;
            }

            var context = Platform.AppContext;
            if (context is null)
            {
                return false;
            }

            return context.CheckSelfPermission(Android.Manifest.Permission.WriteExternalStorage)
                   == Android.Content.PM.Permission.Granted;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>外部存储已挂载且公共 Download 目录真实存在并可写入。</summary>
    private static bool IsAndroidPublicDownloadWritable()
    {
        try
        {
            string state = Android.OS.Environment.ExternalStorageState ?? string.Empty;
            if (!string.Equals(state, "mounted", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            if (!Directory.Exists(AndroidPublicDownload))
            {
                return false;
            }

            return CanCreateProbe(AndroidPublicDownload);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>在 <paramref name="parent"/> 下真实建一个临时目录+文件，验证可写后立即删除。</summary>
    private static bool CanCreateProbe(string parent)
    {
        string probeDirectory = Path.Combine(parent, ProbeName);

        try
        {
            Directory.CreateDirectory(probeDirectory);

            string probeFile = Path.Combine(probeDirectory, "probe.txt");
            File.WriteAllText(probeFile, "probe");
            File.Delete(probeFile);

            return true;
        }
        catch
        {
            return false;
        }
        finally
        {
            try
            {
                if (Directory.Exists(probeDirectory))
                {
                    Directory.Delete(probeDirectory, true);
                }
            }
            catch
            {
                // 清理失败不影响结论
            }
        }
    }

    /// <summary>App 专属外部目录（Android/data/&lt;pkg&gt;/files），不可用时退回应用私有目录。</summary>
    private static string AndroidExternalAppDirectory()
    {
        try
        {
            var context = Platform.AppContext;
            if (context is not null)
            {
                var externalFiles = context.GetExternalFilesDir(null);
                string? absolutePath = externalFiles?.AbsolutePath;

                if (!string.IsNullOrWhiteSpace(absolutePath) && Directory.Exists(absolutePath))
                {
                    return absolutePath!;
                }
            }
        }
        catch
        {
            // 忽略，退回应用私有目录
        }

        return SafeAppDataDirectory();
    }

    private static async Task<bool> RequestAndroidPublicStorageAccessAsync()
    {
        try
        {
            if (HasPublicStorageAccess)
            {
                return true;
            }

            var activity = Platform.CurrentActivity;
            var context = Platform.AppContext ?? (Android.Content.Context?)activity;

            if (OperatingSystem.IsAndroidVersionAtLeast(30))
            {
                if (context is null && activity is null)
                {
                    return false;
                }

                using var intent = new Android.Content.Intent(
                    Android.Provider.Settings.ActionManageAppAllFilesAccessPermission);

                // 只针对本应用的授权页面（部分 ROM 会忽略 data，仍落在应用列表页）。
                intent.SetData(Android.Net.Uri.Parse("package:" + (context?.PackageName ?? activity?.PackageName ?? string.Empty)));

                if (activity is not null)
                {
                    activity.StartActivity(intent);
                }
                else
                {
                    intent.AddFlags(Android.Content.ActivityFlags.NewTask);
                    context!.StartActivity(intent);
                }

                // 给用户/系统一点时间完成切换（用户也可能按返回键不授权）。
                await Task.Delay(500);

                return Android.OS.Environment.IsExternalStorageManager;
            }

            // API < 30：走旧版存储写权限的运行时申请。
            var status = await Permissions.RequestAsync<Permissions.StorageWrite>();
            return status == PermissionStatus.Granted;
        }
        catch
        {
            // 没有 Activity、目标设置页不存在、权限对话框被拒绝等，统一返回 false。
            return false;
        }
    }
#endif
}
