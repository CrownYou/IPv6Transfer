using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Ipv6Transfer.Core.Protocol;
using Ipv6Transfer.Mobile.Models;
using Ipv6Transfer.Mobile.Services;
using Microsoft.Maui.Controls.Shapes;
using IOPath = System.IO.Path;
using Microsoft.Maui.Graphics;

namespace Ipv6Transfer.Mobile.Views;

/// <summary>弹窗里复用的控件构造小工具。</summary>
internal static class Ui
{
    public static Label Text(string text, double size = 14, bool bold = false, Color? color = null)
        => new()
        {
            Text = text,
            FontSize = size,
            FontAttributes = bold ? FontAttributes.Bold : FontAttributes.None,
            TextColor = color ?? Color.FromArgb("#202124"),
            LineBreakMode = LineBreakMode.WordWrap,
        };

    public static Label Muted(string text, double size = 12)
        => Text(text, size, color: AppColors.Muted);

    public static Button Action(string text, EventHandler clicked, bool filled = false)
    {
        var button = new Button
        {
            Text = text,
            FontSize = 14,
            Padding = new Thickness(10, 10),
            CornerRadius = 10,
            BackgroundColor = filled ? AppColors.Listen : Color.FromArgb("#FFFFFF"),
            TextColor = filled ? Colors.White : AppColors.Listen,
            BorderColor = filled ? Colors.Transparent : AppColors.Listen,
            BorderWidth = filled ? 0 : 1,
        };
        button.Clicked += clicked;
        return button;
    }

    public static Border Card(View content, string? title = null)
    {
        var stack = new VerticalStackLayout { Spacing = 8 };
        if (!string.IsNullOrEmpty(title))
        {
            stack.Add(Text(title!, 13, bold: true, color: AppColors.Muted));
        }

        stack.Add(content);
        return new Border
        {
            Padding = new Thickness(12),
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeThickness = 1,
            StrokeShape = new RoundRectangle { CornerRadius = 12 },
            BackgroundColor = Colors.White,
            Content = stack,
        };
    }

    public static Button Toggle(string text, bool selected)
    {
        return new Button
        {
            Text = text,
            FontSize = 14,
            Padding = new Thickness(6, 8),
            CornerRadius = 10,
            BackgroundColor = selected ? AppColors.Listen : Color.FromArgb("#F1F3F4"),
            TextColor = selected ? Colors.White : Color.FromArgb("#202124"),
            BorderWidth = 0,
        };
    }
}

/// <summary>手机文件浏览器：mode=文件 或 文件夹。</summary>
public sealed class FileBrowserPage : ContentPage
{
    private sealed record BrowseEntry(string Name, string FullPath, bool IsDirectory)
    {
        public string Display => IsDirectory ? Name + "/" : Name;
    }

    private readonly bool _directoryMode;
    private readonly TaskCompletionSource<string?> _result = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Entry _pathEntry;
    private readonly CollectionView _list;
    private readonly List<BrowseEntry> _entries = new();
    private readonly List<string> _entryLabels = new();
    private BrowseEntry? _selected;
    private bool _closed;

    public FileBrowserPage(bool directoryMode, string startPath)
    {
        _directoryMode = directoryMode;
        Title = directoryMode ? "选择文件夹" : "选择文件";
        BackgroundColor = Colors.White;

        _pathEntry = new Entry
        {
            Text = startPath,
            FontSize = 13,
            Placeholder = "输入目录路径",
        };
        // 直接用字符串列表（绑定到 "." 自身，不做成员反射），显式指定文字颜色，
        // 这样即使系统深色模式下也不会出现白字白底。
        _list = new CollectionView
        {
            ItemsSource = _entryLabels,
            SelectionMode = SelectionMode.Single,
            ItemTemplate = new DataTemplate(() =>
            {
                var label = new Label
                {
                    FontSize = 15,
                    Padding = new Thickness(12, 10),
                    TextColor = Color.FromArgb("#202124"),
                    BackgroundColor = Colors.Transparent,
                    LineBreakMode = LineBreakMode.TailTruncation,
                };
                label.SetBinding(Label.TextProperty, ".");
                return label;
            }),
        };
        _list.SelectionChanged += (_, e) =>
        {
            var text = e.CurrentSelection.FirstOrDefault() as string;
            var index = text is null ? -1 : _entryLabels.IndexOf(text);
            _selected = index >= 0 && index < _entries.Count ? _entries[index] : null;
        };

        var root = new Grid
        {
            Padding = new Thickness(12),
            RowSpacing = 8,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };

        root.Add(Ui.Text(directoryMode ? "选择文件夹" : "选择要发送的文件", 17, bold: true), 0, 0);

        var pathRow = new Grid
        {
            ColumnSpacing = 6,
            ColumnDefinitions = { new ColumnDefinition(GridLength.Star), new ColumnDefinition(GridLength.Auto) },
        };

        pathRow.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Content = _pathEntry,
        }, 0, 0);
        var go = Ui.Action("前往", (_, _) => LoadPath(_pathEntry.Text));
        pathRow.Add(go, 1, 0);
        root.Add(pathRow, 0, 1);

        var quick = new Grid
        {
            ColumnSpacing = 6,
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        quick.Add(Ui.Action("手机存储", (_, _) => LoadPath("/storage/emulated/0")), 0, 0);
        quick.Add(Ui.Action("下载", (_, _) => LoadPath("/storage/emulated/0/Download")), 1, 0);
        quick.Add(Ui.Action("主目录", (_, _) => LoadPath(GetHome())), 2, 0);
        quick.Add(Ui.Action("上一级", (_, _) => LoadPath(GetParent(_pathEntry.Text))), 3, 0);
        root.Add(quick, 0, 2);

        root.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Content = _list,
        }, 0, 3);

        var bottom = new Grid
        {
            ColumnSpacing = 6,
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        bottom.Add(Ui.Action("进入目录", (_, _) => OpenSelected()), 0, 0);
        bottom.Add(Ui.Action("选择", (_, _) => Choose(), filled: true), 1, 0);
        bottom.Add(Ui.Action("取消", (_, _) => Close(null)), 2, 0);
        root.Add(bottom, 0, 4);

        Content = root;
        LoadPath(startPath);
    }


    public Task<string?> Result => _result.Task;

    private static string GetHome()
    {
        try
        {
            var home = Environment.GetFolderPath(Environment.SpecialFolder.Personal);
            return string.IsNullOrEmpty(home) ? "/storage/emulated/0" : home;
        }
        catch (Exception)
        {
            return "/storage/emulated/0";
        }
    }

    private static string GetParent(string? path)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return "/";
            }

            var parent = IOPath.GetDirectoryName(path.TrimEnd(IOPath.DirectorySeparatorChar));
            return string.IsNullOrEmpty(parent) ? "/" : parent;
        }
        catch (Exception)
        {
            return "/";
        }
    }

    private void OpenSelected()
    {
        if (_selected is null)
        {
            return;
        }

        if (_selected.IsDirectory)
        {
            LoadPath(_selected.FullPath);
        }
        else if (!_directoryMode)
        {
            Choose();
        }
    }

    private void Choose()
    {
        if (_directoryMode)
        {
            Close(string.IsNullOrWhiteSpace(_pathEntry.Text) ? null : _pathEntry.Text);
            return;
        }

        if (_selected is null || _selected.IsDirectory)
        {
            _ = DisplayAlertAsync("请选择文件", "请先点选一个文件，再点「选择」。", "好");
            return;
        }

        Close(_selected.FullPath);
    }

    private void LoadPath(string? path)
    {
        var target = (path ?? string.Empty).Trim();
        if (target.Length == 0)
        {
            target = _directoryMode
                ? MobileStorage.DefaultDirectory("received")
                : GetHome();
        }

        try
        {
            var full = IOPath.GetFullPath(target);
            var directories = Directory.EnumerateDirectories(full)
                .Select(item => new BrowseEntry(IOPath.GetFileName(item), item, true))
                .OrderBy(item => item.Name, StringComparer.OrdinalIgnoreCase);
            var files = _directoryMode
                ? Enumerable.Empty<BrowseEntry>()
                : Directory.EnumerateFiles(full)
                    .Select(item => new BrowseEntry(IOPath.GetFileName(item), item, false))
                    .OrderBy(item => item.Name, StringComparer.OrdinalIgnoreCase);

            _entries.Clear();
            _entryLabels.Clear();
            foreach (var entry in directories.Concat(files))
            {
                _entries.Add(entry);
                _entryLabels.Add(entry.Display);
            }

            _pathEntry.Text = full;
            _list.ItemsSource = null;
            _list.ItemsSource = _entryLabels;
            _selected = null;
        }
        catch (Exception ex)
        {
            _ = DisplayAlertAsync("无法打开目录", $"{target}\n{ex.Message}", "好");
        }
    }

    private void Close(string? value)
    {
        if (_closed)
        {
            return;
        }

        _closed = true;
        _result.TrySetResult(value);
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close(null);
        return true;
    }
}

/// <summary>通用输入弹窗（单行 / 多行，可选第二个按钮）。</summary>
public sealed class PromptPage : ContentPage
{
    private readonly TaskCompletionSource<string?> _result = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Editor _editor;
    private readonly string? _secondaryLabel;
    private bool _closed;

    public PromptPage(string title, string prompt, string initial, bool multiline, string accept, string? secondaryLabel)
    {
        Title = title;
        BackgroundColor = Colors.White;
        _secondaryLabel = secondaryLabel;

        _editor = new Editor
        {
            Text = initial,
            FontSize = 15,
            AutoSize = EditorAutoSizeOption.TextChanges,
            HeightRequest = multiline ? 140 : 48,
        };
        if (!multiline)
        {
            _editor.AutoSize = EditorAutoSizeOption.Disabled;
        }

        var root = new Grid
        {
            Padding = new Thickness(16),
            RowSpacing = 10,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };
        root.Add(Ui.Text(title, 17, bold: true), 0, 0);
        root.Add(Ui.Muted(prompt), 0, 1);
        root.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Content = _editor,
            VerticalOptions = LayoutOptions.Start,
        }, 0, 2);

        var buttons = new Grid
        {
            ColumnSpacing = 8,
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        var column = 0;
        buttons.Add(Ui.Action(accept, (_, _) => Close(_editor.Text?.Trim()), filled: true), column++, 0);
        if (!string.IsNullOrEmpty(secondaryLabel))
        {
            buttons.Add(Ui.Action(secondaryLabel!, (_, _) => Close(string.Empty)), column++, 0);
        }

        buttons.Add(Ui.Action("取消", (_, _) => Close(null)), column, 0);
        root.Add(buttons, 0, 3);

        Content = root;
    }

    public Task<string?> Result => _result.Task;

    private void Close(string? value)
    {
        if (_closed)
        {
            return;
        }

        _closed = true;
        _result.TrySetResult(value);
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close(null);
        return true;
    }

    protected override void OnAppearing()
    {
        base.OnAppearing();
        _editor.Focus();
    }
}

/// <summary>文件 / 传输记录详情弹窗。</summary>
public sealed class DetailPage : ContentPage
{
    private readonly TaskCompletionSource _closed = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly FileDetailInfo _info;

    public DetailPage(FileDetailInfo info)
    {
        _info = info;
        Title = info.Title;
        BackgroundColor = Colors.White;

        var nameLabel = new Label
        {
            Text = info.Name,
            FontFamily = "monospace",
            FontSize = 13,
            LineBreakMode = LineBreakMode.CharacterWrap,
        };

        var meta = new VerticalStackLayout { Spacing = 4 };
        foreach (var pair in info.Meta)
        {
            var row = new Grid
            {
                ColumnSpacing = 8,
                ColumnDefinitions = { new ColumnDefinition(90), new ColumnDefinition(GridLength.Star) },
            };
            row.Add(Ui.Muted(pair.Key), 0, 0);
            row.Add(Ui.Text(pair.Value, 13), 1, 0);
            meta.Add(row);
        }

        var root = new Grid
        {
            Padding = new Thickness(16),
            RowSpacing = 10,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };

        root.Add(Ui.Text(info.Title, 17, bold: true), 0, 0);
        root.Add(Ui.Muted("完整文件名"), 0, 1);
        root.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Padding = 8,
            Content = nameLabel,
        }, 0, 2);
        root.Add(new ScrollView { Content = meta }, 0, 3);

        var buttons = new Grid
        {
            ColumnSpacing = 8,
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        buttons.Add(Ui.Action("复制文件名", async (_, _) => await CopyAsync(info.Name)), 0, 0);
        buttons.Add(Ui.Action("关闭", (_, _) => Close()), 1, 0);

        var extra = new Grid
        {
            ColumnSpacing = 8,
            Margin = new Thickness(0, 8, 0, 0),
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        var extraColumn = 0;
        if (!string.IsNullOrEmpty(info.CopyPath))
        {
            extra.Add(Ui.Action("复制路径", async (_, _) => await CopyAsync(info.CopyPath!)), extraColumn++, 0);
        }

        if (info.HashRow is not null)
        {
            extra.Add(Ui.Action("哈希详情", async (_, _) =>
            {
                Close();
                await DialogService.Instance.ShowHashDetailAsync(info.HashRow!);
            }), extraColumn, 0);
        }

        var stack = new VerticalStackLayout { Spacing = 0 };
        stack.Add(buttons);
        if (extraColumn > 0)
        {
            stack.Add(extra);
        }

        root.Add(stack, 0, 4);
        Content = root;
    }

    public Task Closed => _closed.Task;

    private async Task CopyAsync(string value)
    {
        if (await DialogService.Instance.CopyAsync(value))
        {
            await DisplayAlertAsync("已复制", "内容已复制到剪贴板。", "好");
        }
    }

    private void Close()
    {
        _closed.TrySetResult();
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close();
        return true;
    }
}

/// <summary>SHA-256 对比弹窗。</summary>
public sealed class HashDetailPage : ContentPage
{
    private readonly TaskCompletionSource _closed = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public HashDetailPage(TransferRow row)
    {
        Title = "哈希详情";
        BackgroundColor = Colors.White;

        var local = string.IsNullOrEmpty(row.LocalSha256) ? "（未计算）" : row.LocalSha256;
        var peer = string.IsNullOrEmpty(row.PeerSha256) ? "（对端未提供）" : row.PeerSha256;
        var verdict = row.Matched switch
        {
            true => "一致：两边文件完全相同",
            false => "不一致，请检查传输过程",
            null => "传输完成后显示比对结果",
        };

        var root = new Grid
        {
            Padding = new Thickness(16),
            RowSpacing = 10,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };

        root.Add(Ui.Text("哈希详情", 17, bold: true), 0, 0);

        var rows = new VerticalStackLayout { Spacing = 8 };
        rows.Add(HashRow("本机 SHA-256", local));
        rows.Add(HashRow("对端 SHA-256", peer));
        root.Add(rows, 0, 1);

        root.Add(Ui.Text(verdict, 15, bold: true, color: row.Matched == false ? AppColors.Bad : AppColors.Ok), 0, 2);
        root.Add(Ui.Muted($"文件：{row.Name}\n大小：{row.SizeText}"), 0, 3);
        root.Add(Ui.Action("关闭", (_, _) => Close()), 0, 4);

        Content = root;
    }

    public Task Closed => _closed.Task;

    private View HashRow(string label, string value)
    {
        var grid = new Grid
        {
            ColumnSpacing = 6,
            ColumnDefinitions =
            {
                new ColumnDefinition(92),
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Auto),
            },
        };
        grid.Add(Ui.Muted(label), 0, 0);
        grid.Add(new Label
        {
            Text = value,
            FontFamily = "monospace",
            FontSize = 11,
            LineBreakMode = LineBreakMode.CharacterWrap,
        }, 1, 0);
        grid.Add(Ui.Action("复制", async (_, _) =>
        {
            if (await DialogService.Instance.CopyAsync(value))
            {
                await DisplayAlertAsync("已复制", "已复制到剪贴板。", "好");
            }
        }), 2, 0);
        return grid;
    }

    private void Close()
    {
        _closed.TrySetResult();
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close();
        return true;
    }
}

/// <summary>服务器「分享配置」弹窗。</summary>
public sealed class ShareConfigPage : ContentPage
{
    private readonly TaskCompletionSource _closed = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public ShareConfigPage(string token, string hint)
    {
        Title = "连接配置";
        BackgroundColor = Colors.White;

        var editor = new Editor
        {
            Text = token,
            IsReadOnly = true,
            FontFamily = "monospace",
            FontSize = 12,
            HeightRequest = 140,
        };

        var root = new Grid
        {
            Padding = new Thickness(16),
            RowSpacing = 10,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };
        root.Add(Ui.Text("连接配置", 17, bold: true), 0, 0);
        root.Add(Ui.Muted("把整段发给客户端，客户端点「导入配置」粘贴即可："), 0, 1);
        root.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Content = editor,
        }, 0, 2);
        root.Add(new ScrollView { Content = Ui.Muted(hint, 13) }, 0, 3);

        var buttons = new Grid
        {
            ColumnSpacing = 8,
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        buttons.Add(Ui.Action("复制连接串", async (_, _) =>
        {
            if (await DialogService.Instance.CopyAsync(token))
            {
                await DisplayAlertAsync("已复制", "连接串已复制到剪贴板。", "好");
            }
        }, filled: true), 0, 0);
        buttons.Add(Ui.Action("关闭", (_, _) => Close()), 1, 0);
        root.Add(buttons, 0, 4);

        Content = root;
    }

    public Task Closed => _closed.Task;

    private void Close()
    {
        _closed.TrySetResult();
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close();
        return true;
    }
}

/// <summary>客户端「导入配置」弹窗。</summary>
public sealed class ImportConfigPage : ContentPage
{
    private readonly TaskCompletionSource<string?> _result = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Editor _editor;
    private readonly Label _hintLabel;
    private bool _closed;

    public ImportConfigPage(string initial, string hint)
    {
        Title = "导入连接配置";
        BackgroundColor = Colors.White;

        _editor = new Editor { Text = initial, FontFamily = "monospace", FontSize = 12, HeightRequest = 150 };
        _hintLabel = Ui.Muted(hint, 12);

        var root = new Grid
        {
            Padding = new Thickness(16),
            RowSpacing = 8,
            RowDefinitions =
            {
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Auto),
                new RowDefinition(GridLength.Star),
                new RowDefinition(GridLength.Auto),
            },
        };
        root.Add(Ui.Text("导入连接配置", 17, bold: true), 0, 0);
        root.Add(Ui.Text("粘贴服务器发来的 FT6. 开头连接串：", 14), 0, 1);
        root.Add(_hintLabel, 0, 2);
        root.Add(new Border
        {
            Stroke = new SolidColorBrush(Color.FromArgb(AppColors.BorderHex)),
            StrokeShape = new RoundRectangle { CornerRadius = 8 },
            Content = _editor,
        }, 0, 3);

        var bar = new Grid
        {
            RowSpacing = 6,
            ColumnSpacing = 8,
            RowDefinitions = { new RowDefinition(GridLength.Auto), new RowDefinition(GridLength.Auto) },
            ColumnDefinitions =
            {
                new ColumnDefinition(GridLength.Star),
                new ColumnDefinition(GridLength.Star),
            },
        };
        bar.Add(Ui.Action("读取剪贴板", async (_, _) =>
        {
            var text = await DialogService.Instance.ReadClipboardAsync();
            if (string.IsNullOrEmpty(text))
            {
                await DisplayAlertAsync("剪贴板不可用", "没有读到剪贴板文本。\n请长按输入框手动粘贴，或点「从文件导入」。", "好");
                return;
            }

            _editor.Text = text;
            _hintLabel.Text = "已读取剪贴板，请点「导入」。";
        }), 0, 0);

        bar.Add(Ui.Action("从文件导入", async (_, _) =>
        {
            var path = await DialogService.Instance.PickPathAsync(false, MobileStorage.DisplayRoot);
            if (string.IsNullOrEmpty(path))
            {
                return;
            }

            try
            {
                _editor.Text = File.ReadAllText(path).Trim();
                _hintLabel.Text = $"已读取文件：{IOPath.GetFileName(path)}，请点「导入」。";
            }
            catch (Exception ex)
            {
                await DisplayAlertAsync("读取失败", ex.Message, "好");
            }
        }), 1, 0);

        bar.Add(Ui.Action("导入", (_, _) => Close(_editor.Text?.Trim()), filled: true), 0, 1);
        bar.Add(Ui.Action("取消", (_, _) => Close(null)), 1, 1);
        root.Add(bar, 0, 4);

        Content = root;
    }

    public Task<string?> Result => _result.Task;

    private void Close(string? value)
    {
        if (_closed)
        {
            return;
        }

        _closed = true;
        _result.TrySetResult(value);
        _ = Navigation.PopModalAsync();
    }

    protected override bool OnBackButtonPressed()
    {
        Close(null);
        return true;
    }
}
