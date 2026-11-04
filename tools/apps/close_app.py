from agent.process import run_applescript

_QUIT_SCRIPT = '''
on run argv
    set appName to item 1 of argv

    if not (application appName is running) then
        error "起動していないアプリです: " & appName
    end if

    tell application appName to quit
end run
'''


def close_app(name):
    """アプリに通常の終了を要求する(名前をパターンとして扱って強制終了しない)。"""

    run_applescript(_QUIT_SCRIPT, name)
