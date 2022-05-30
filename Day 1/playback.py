#playback



from typing_extensions import Self


def playback(self):
        options = input("Type something!\n")
        ch = " "
        if ch in options:
            print(options.replace(" ", "..."))
        else:
            print(options)

playback(Self)
