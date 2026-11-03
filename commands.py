from agent.models import Command


COMMANDS = {

    "メモ":

        Command(

            tool="open_app",

            args={

                "name": "Notes"

            },

            message="📒 メモを開きました"

        ),

    "クローム":

        Command(

            tool="open_app",

            args={

                "name": "Google Chrome"

            },

            message="🌐 Chromeを開きました"

        )

}