#!/bin/python3

#Simple port scanner

from email.policy import default
import sys
import socket
from datetime import datetime

#Defines target
if len(sys.argv) == 2:
    target = socket.gethostbyname(sys.argv[1]) #Translate host name to ipv4
else:
    print("Invalid amount of arguements.")
    print("Synatax: python3 scanner.py <ip>")

#Banner
print("-" * 50)
print("Scanning target " + target)
print("Time Started " + str(datetime.now()))
print("-" * 50)

try:
    for port in range(50,85): #Would be better with threading
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket.setdefaulttimeout(1)
        result = s.connect_ex((target,port))
        if result == 0:
            print("Port {} is open".format(port))
        s.close
except KeyboardInterrupt:
        print("\n Exiting program.")
        sys.exit
except socket.gaierror:
        print("Hostname could not be resolved.")
        sys.exit
except socket.error:
        print("Couldn't connect to server.")
        sys.exit
