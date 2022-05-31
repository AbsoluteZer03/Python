#Interpreter

from array import array


def main():
    math = input("Give me an arthimatic expression.")
    x, y, z = math.split(" ")
    x = float(x)
    z = float(z)
    if y == "+":
        calc = x + z
    elif y == "-":
        calc = x - z
    elif y == "*":
        calc = x * z
    elif y == "/":
        calc = x / z
    else:
        print("This is not a valid arthimatic expression.")
    print(round(calc, 1))

main()