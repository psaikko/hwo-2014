import json
import socket
import sys
import math
from time import clock

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
    def __init__(self, json, lanes):
        if 'angle' in json:
            self.turn = True
            self.radius = json['radius']
            self.angle = json['angle']
            m = -1 if self.angle > 0 else 1
            self.lengths = [abs(math.pi * self.angle / 180.0 * (self.radius + m*lane.offset)) for lane in lanes]
        else:
            self.turn = False
            self.const_length = json['length']
        self.switch = 'switch' in json

    def length(self, lane=0):
        if not self.turn:
            return self.const_length
        else:
            return self.lengths[lane]


    def __repr__(self):
        s = " with switch" if self.switch else ""
        if self.turn:
            return "Turn of angle %.3f, radius %.3f, with length %.3f%s" % (self.angle, self.radius, self.length(), s)
        else:
            return "Straight of length %d%s" % (self.length(), s)

class Track:
    def __init__(self, json):
        self.id = json['id']
        self.name = json['name']
        self.lanes = [Lane(l) for l in json['lanes']]
        self.pieces = [Piece(p, self.lanes) for p in json['pieces']]
        
    def __repr__(self):
        s = self.name+'\n'
        s += '\n'.join([p.__repr__() for p in self.pieces]) + '\n'
        s += '\n'.join([l.__repr__() for l in self.lanes])
        return s

class Position:
    def __init__(self, track, json):
        self.angle = json['angle']
        piece_pos = json['piecePosition']
        self.piece_idx = piece_pos['pieceIndex']
        self.piece = track.pieces[self.piece_idx]
        self.piece_dist = piece_pos['inPieceDistance']
        self.start_lane_idx = piece_pos['lane']['startLaneIndex']
        self.end_lane_idx = piece_pos['lane']['endLaneIndex']

    def __repr__(self):
        return "%d/%d on piece %d, (%d-%d), at angle %.2f" % (self.piece_dist, self.piece.length(self.start_lane_idx), self.piece_idx, self.start_lane_idx, self.end_lane_idx, self.angle)

class ProBot(object):
    def __init__(self, socket, name, key):
        self.socket = socket
        self.name = name
        self.key = key
        self.ticks = 0
        self.next_switch_piece = None
        self.last_position = None
        self.last_v = 0
        self.times = []

    def msg(self, msg_type, data):
        self.send(json.dumps({"msgType": msg_type, "data": data}))

    def send(self, msg):
        self.socket.sendall(msg + "\n")

    def run(self, track=""):
        self.join(track)
        self.msg_loop()

    def join(self, track):
        if track == "": 
            return self.msg("join", {"name": self.name,
                                     "key": self.key})
        else:
            data = {"botId": {"name": self.name,
                              "key": self.key},
                    "trackName": track,
                    "carCount": 1}
            print(data)
            return self.msg("joinRace", data)

    def throttle(self, throttle):
        self.msg("throttle", throttle)

    def switch(self, direction):
        self.msg("switchLane", direction)

    def ping(self):
        print('.. ping ..')
        self.msg("ping", {})

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
        print(race['raceSession'])
        self.session = Session(race['raceSession'])
        print(self.track)
        print(self.session)
        self.cars = race['cars']

    def on_game_start(self, data):
        print("Race started")
        self.ping()

    def on_crash(self, data):
        if data['color'] == self.color:
            print("I crashed")
        else:
            print("Someone crashed")
        self.ping()

    def on_spawn(self, data):
        if data['color'] == self.color:
            print("I spawned")
        else:
            print("Someone spawned")
        self.ping()

    def on_game_end(self, data):
        print("Race ended")
        self.ping()

    def on_error(self, data):
        print("Error: {0}".format(data))
        self.ping()

    def on_lap_finished(self, data):
        c = data['car']['color']
        t = data['lapTime']['millis']
        print("======= %s: %d ms =======" % (c, t))
        self.ping()

    def on_car_positions(self, data):
        cars = dict()
        for json in data:
            cars[json['id']['color']] = Position(self.track, json)
        own_position = cars[self.color]

        vel, acc, da = 0, 0, 0

        if self.last_position != None:
            da = own_position.angle - self.last_position.angle
            if self.last_position.piece == own_position.piece:
                vel = own_position.piece_dist - self.last_position.piece_dist
            else:
                start_idx = self.last_position.start_lane_idx
                end_idx = self.last_position.end_lane_idx
                # lets not try figure out lane change track lengths...
                if start_idx != end_idx:
                    vel = self.last_v
                else:
                    vel = self.last_position.piece.length(start_idx) - self.last_position.piece_dist + own_position.piece_dist

        acc = vel - self.last_v
        self.last_position = own_position
        self.last_v = vel
        self.ticks += 1

        # logic for switching to the shortest lane
        if self.next_switch_piece in [None, own_position.piece] and own_position.piece_dist > (own_position.piece.length() / 2):

            i = own_position.piece_idx
            n = len(self.track.pieces)
            
            switch_idx = None
            for j in range(1,n):
                p = self.track.pieces[(i+j)%n]
                if p.switch:
                    self.next_switch_piece = p
                    switch_idx = (i+j)%n
                    break
            print("Next switch: %d" % (switch_idx,))
            right_turns = 0
            left_turns = 0
            for j in range(1,n):
                p = self.track.pieces[(switch_idx+j)%n]
                if p.switch:
                    break
                if p.turn:
                    if p.angle > 0:
                        print("Right at %d" % ((switch_idx+j)%n,))
                        right_turns += 1
                    else: 
                        print("Left at %d" % ((switch_idx+j)%n,))
                        left_turns += 1
            if right_turns > left_turns:
                print("switch right")
                self.switch("Right")
                return
            if left_turns > right_turns:
                print("switch left")
                self.switch("Left")
                return

        # some crude throttle control with magic numbers
        drift = own_position.angle
        if drift > 55 and own_position.piece.turn:
            self.throttle(0)
        else:
            if (self.track.pieces[(own_position.piece_idx + 1) % len(self.track.pieces)].turn or\
                    own_position.piece.turn) and vel > 6.5:
                self.throttle(0)
            elif own_position.piece.turn and drift > 50:
                self.throttle(0.65)
            else:
                self.throttle(1)

        turn_radius = 0 if not own_position.piece.turn else own_position.piece.radius
        if self.ticks % 10 == 0:
            print(own_position)
            drift_log = "#%.3f,%.3f,%d" % (vel, da, turn_radius)
            print(drift_log)

    def msg_loop(self):
        msg_map = {
            'join': self.on_join,
            'gameStart': self.on_game_start,
            'yourCar': self.on_car_id,
            'gameInit': self.on_game_init,
            'carPositions': self.on_car_positions,
            'crash': self.on_crash,
            'spawn': self.on_spawn,
            'gameEnd': self.on_game_end,
            'error': self.on_error,
            'lapFinished': self.on_lap_finished
        }
        socket_file = s.makefile()
        line = socket_file.readline()
        times = []
        while line:
            before = clock()
            msg = json.loads(line)
            msg_type, data = msg['msgType'], msg['data']
            if msg_type in msg_map:
                msg_map[msg_type](data)
                if msg_type == 'gameEnd':
                    break
            else:
                print("Got {0}".format(msg_type))
                self.ping()
            after = clock()
            line = socket_file.readline()
            times.append(after - before)
        print("time pondering:")
        print("min", min(times))
        print("max", max(times))
        print("avg", sum(times) / len(times))


if __name__ == "__main__":
    if len(sys.argv) == 6:
        host, port, name, key, track = sys.argv[1:6]
        print("Connecting with parameters:")
        print("host={0}, port={1}, bot name={2}, key={3}, track={4}".format(*sys.argv[1:6]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, int(port)))
        bot = ProBot(s, name, key)
        bot.run(track)
    elif len(sys.argv) != 5:
        print("Usage: ./run host port botname botkey")
    else:
        host, port, name, key = sys.argv[1:5]
        print("Connecting with parameters:")
        print("host={0}, port={1}, bot name={2}, key={3}".format(*sys.argv[1:5]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, int(port)))
        bot = ProBot(s, name, key)
        bot.run()