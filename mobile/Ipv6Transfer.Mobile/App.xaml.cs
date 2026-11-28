using Ipv6Transfer.Mobile.Services;
using Ipv6Transfer.Mobile.ViewModels;
using Ipv6Transfer.Mobile.Views;

namespace Ipv6Transfer.Mobile;

public partial class App : Application
{
    private MainViewModel? _viewModel;

    public App()
    {
        InitializeComponent();

        // 界面配色是按浅色设计的（和 python 版一致）。系统深色模式下若跟随主题，
        // 原生控件（RadioButton / 列表默认文字）会变成深底白字，与白色卡片冲突，
        // 所以这里固定为浅色主题。
        UserAppTheme = AppTheme.Light;
    }

    protected override Window CreateWindow(IActivationState? activationState)
    {
        _viewModel = new MainViewModel(DialogService.Instance);
        var page = new MainPage(_viewModel);
        var window = new Window(new NavigationPage(page))
        {
            Title = "IPv6 文件传输",
        };
        window.Destroying += (_, _) => _viewModel?.Dispose();
        window.Stopped += (_, _) => _viewModel?.SaveConfig();
        return window;
    }
}
