import json
import socket
import sys
import math

class Session:
    def __init__(self, json):
        self.laps = json['laps']
        self.cutoff = json['maxLapTimeMs']
        self.is_quick = json['quickRace']

    def __repr__(self):
        t = "Quick race" if self.is_quick else "Session"
        return "%s of %d laps, with %d ms cutoff" % (t, self.laps, self.cutoff)

class Lane:
    def __init__(self, json):
        self.index = json['index']
        self.offset = json['distanceFromCenter']

    def __repr__(self):
        return "Lane %d, offset %d" % (self.index, self.offset)

class Piece:
    def __init__(self, json):
        if 'angle' in json:
            self.turn = True
            self.radius = json['radius']
            self.angle = json['angle']
            self.length = (math.pi * self.angle / 180.0) * self.radius
        else:
            self.turn = False
            self.length = json['length']
        self.switch = 'switch' in json

    def __repr__(self):
        if self.turn:
            return "Turn of angle %f, radius %f, with length %d" % (self.angle, self.radius, self.length)
        else:
            return "Straight of length %d%s" % (self.length, " with switch" if self.switch else "")

class Track:
    def __init__(self, json):
        self.id = json['id']
        self.name = json['name']
        self.pieces = [Piece(p) for p in json['pieces']]
        self.lanes = [Lane(l) for l in json['lanes']]

    def __repr__(self):
        s = self.name+'\n'
        s += '\n'.join([p.__repr__() for p in self.pieces]) + '\n'
        s += '\n'.join([l.__repr__() for l in self.lanes]) + '\n'
        return s

class ProBot(object):
    def __init__(self, socket, name, key):
        self.socket = socket
        self.name = name
        self.key = key

    def msg(self, msg_type, data):
        self.send(json.dumps({"msgType": msg_type, "data": data}))

    def send(self, msg):
        self.socket.sendall(msg + "\n")

    def join(self):
        return self.msg("join", {"name": self.name,
                                 "key": self.key})

    def throttle(self, throttle):
        self.msg("throttle", throttle)

    def ping(self):
        self.msg("ping", {})

    def run(self):
        self.join()
        self.msg_loop()

    def on_join(self, data):
        print("Joined")
        self.ping()

    def on_car_id(self, data):
        color = data['color']
        print("Identified as " + color)
        self.color = color

    def on_game_init(self, data):
        race = data['race']
        self.track = Track(race['track'])
        self.session = Session(race['raceSession'])
        print(self.track)
        print(self.session)
        self.cars = race['cars']

    def on_game_start(self, data):
        print("Race started")
        self.ping()

    def on_car_positions(self, data):
        self.throttle(0.6)

    def on_crash(self, data):
        print("Someone crashed")
        self.ping()

    def on_game_end(self, data):
        print("Race ended")
        self.ping()

    def on_error(self, data):
        print("Error: {0}".format(data))
        self.ping()

    def msg_loop(self):
        msg_map = {
            'join': self.on_join,
            'gameStart': self.on_game_start,
            'yourCar': self.on_car_id,
            'gameInit': self.on_game_init,
            'carPositions': self.on_car_positions,
            'crash': self.on_crash,
            'gameEnd': self.on_game_end,
            'error': self.on_error,
        }
        socket_file = s.makefile()
        line = socket_file.readline()
        while line:
            msg = json.loads(line)
            msg_type, data = msg['msgType'], msg['data']
            if msg_type in msg_map:
                msg_map[msg_type](data)
            else:
                print("Got {0}".format(msg_type))
                self.ping()
            line = socket_file.readline()

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: ./run host port botname botkey")
    else:
        host, port, name, key = sys.argv[1:5]
        print("Connecting with parameters:")
        print("host={0}, port={1}, bot name={2}, key={3}".format(*sys.argv[1:5]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, int(port)))
        bot = ProBot(s, name, key)
        bot.run()