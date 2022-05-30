#Deep Thoughts

def main():
    Answer = input("What is the Answer to the Great Question of Life, the Universe, and Everything? \n")
    if "42" or "forty-two" or "forty two" in Answer.lower:
        print("Yes")
    else:
        print("No")

main()