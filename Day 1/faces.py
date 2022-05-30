#faces

from typing_extensions import Self


def main():
    msg = input("Type something! \n")
    result = convert(msg)
    print(result)

def convert(msg):
    msg =  msg.replace(":)","🙂")
    msg =  msg.replace(":(","🙁")
    return msg

main()
