#!/bin/python3

#simple script to connect to a host on a port and end connection node to node connection.

import socket

HOST = ''
PORT = 7777

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

s.connect((HOST, PORT))

