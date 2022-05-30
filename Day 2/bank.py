#Bank

def main():
    greeting = input("Greeting:")
    greeting = greeting.lower()
    if "hello" in greeting:
        print("$0")
    elif "h" in greeting:
        greeting.replace(" ", "")
        h = greeting.find("h")
        if h == 0:
            print("$20")
    else:
        print("$100")
            
        

main()