import subprocess


def mail_send(to: str, subject: str, body: str):
    """メールを送信する（Mail.app経由）"""

    script = f'''
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:true}}
        tell newMessage
            make new to recipient with properties {{address:"{to}"}}
        end tell
        send newMessage
    end tell
    '''

    subprocess.run(["osascript", "-e", script])
