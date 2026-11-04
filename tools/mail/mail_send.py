from agent.process import run_applescript

_SEND_SCRIPT = '''
on run argv
    set recipientAddress to item 1 of argv
    set messageSubject to item 2 of argv
    set messageBody to item 3 of argv

    tell application "Mail"
        set newMessage to make new outgoing message with properties {subject:messageSubject, content:messageBody, visible:true}
        tell newMessage
            make new to recipient with properties {address:recipientAddress}
        end tell
        send newMessage
    end tell
end run
'''


def mail_send(to: str, subject: str, body: str):
    """メールを送信する（Mail.app経由）"""

    run_applescript(_SEND_SCRIPT, to, subject, body)
