#File Extensions


def main():
    list = [".gif", ".jpg", ".jpeg", ".png",".pdf", ".txt", ".zip"]
    check = str

    file = input("What is the name of your file.")
    file = file.lower()
    if file in list:
        for i in range(7):
            if file in list[i]:
                


main()