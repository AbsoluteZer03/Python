#File Extensions

def main():
    list1 = {".gif":"image/gif", ".jpg":"image/jpg", ".jpeg": "image/jpeg", ".png":"image/png",".pdf":"application/pdf",
     ".txt":"text/plain", ".zip":"application/zip"}
    file = input("What is the name of your file. \n").lower().split(".", 1)[1]
    file = ("." + file)
    if file in list1:
        print(list1[file])
    else:
        print("application/octet-stream")
main()