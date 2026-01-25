from openreward.environments import Server

from seta import SETAEnv

if __name__ == "__main__":
    server = Server([SETAEnv])
    server.run()
