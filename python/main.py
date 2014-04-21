import json
import socket
import sys
import math
from time import clock

# TODO: learn traction
# TODO: lane change logic for corner entry speed
# TODO: calculate straightening rate after corners

MAX_DRIFT_ANGLE = 60
DECELERATION_RATE = 0.02
TRACTION_EST = 0.321
DRIFT_DECAY_RATE_EST = 0.2

def distance_to_target_speed(current, target):
    d = 0
    if target <= 0: return -1
    while current > target:
        d += current
        current *= (1 - DECELERATION_RATE)
    return d

def traction_loss_threshold(radius):
    return math.sqrt(TRACTION_EST * radius)

class Session:
    def __init__(self, json):
        self.laps = json['laps'] if 'laps' in json else None
        self.cutoff = json['maxLapTimeMs'] if 'maxLapTimeMs' in json else None
        self.is_quick = json['quickRace'] if 'quickRace' in json else None
        self.json = json

    def __repr__(self):
        return self.json.__repr__()
        #t = "Quick race" if self.is_quick else "Session"
        #return "%s of %d laps, with %d ms cutoff" % (t, self.laps, self.cutoff)

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

class FooBot(object):
    def __init__(self, socket, name, key):
        self.socket = socket
        self.name = name
        self.key = key
        self.ticks = 0
        self.next_switch_piece = None
        self.next_switch_idx = -1
        self.next_lane = -1
        self.positions = []
        self.vs = [0]
        self.ts = [0]
        self.dts = [0]
        self.ddts = [0]

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
        x = int(round(throttle / 0.1))
        vis = "["+ '='*x + ' '*(10-x) +"]"
        print(vis)
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

    def corner_radius(self, piece, lane):
        turn_radius = 0 if not piece.turn else piece.radius
        if turn_radius > 0:
            if piece.angle > 0:
                turn_radius -= self.track.lanes[lane].offset
            else:
                turn_radius += self.track.lanes[lane].offset
        return turn_radius

    def on_car_positions(self, data):
        cars = dict()
        for json in data:
            cars[json['id']['color']] = Position(self.track, json)
        
        own_pos = cars[self.color]
        last_pos = None

        v, dt = 0, 0
        t = own_pos.angle
        
        if len(self.positions) > 0:
            last_pos = self.positions[-1]
            dt = t - last_pos.angle
            if last_pos.piece == own_pos.piece:
                v = own_pos.piece_dist - last_pos.piece_dist
            else:
                start_idx = last_pos.start_lane_idx
                end_idx = last_pos.end_lane_idx
                # lets not try figure out lane change track lengths...
                if start_idx != end_idx:
                    v = self.vs[-1]
                else:
                    v = last_pos.piece.length(start_idx) - last_pos.piece_dist + own_pos.piece_dist

        dv = v - self.vs[-1]
        ddt = dt - self.dts[-1]
        self.positions.append(own_pos)
        self.vs.append(v)
        self.ts.append(t)
        self.dts.append(dt)
        self.ddts.append(dt)
        self.ticks += 1

        turn_radius = self.corner_radius(own_pos.piece, own_pos.end_lane_idx)
        if self.ticks % 10 == 5:
            print(own_pos)
            drift_log = "v = %.2f, a = %.2f, dt = %.2f, ddt = %.2f, r = %d" % (v, t, dt, ddt, turn_radius)
            print(drift_log)

        # acceleration test
        #print("v %d,%.2f" % (self.ticks, v))
        #print("t %d,%.2f" % (self.ticks, t))
        #print("dt %d,%.2f" % (self.ticks, dt))
        #print("ddt %d,%.2f" % (self.ticks, ddt))

        #print("v-t %.2f,%.2f" % (v, t))
        #print("v-dt %.2f,%.2f" % (v, dt))
        #print("v-ddt %.2f,%.2f" % (v, ddt))
        #return

        if not own_pos.piece.turn and (dt != 0 or ddt != 0):
            print("# %.2f %.2f" % (dt, ddt))

        # logic for switching to the shortest lane
        if self.next_switch_piece in [None, own_pos.piece] and own_pos.piece_dist > (own_pos.piece.length() / 2):

            i = own_pos.piece_idx
            n = len(self.track.pieces)
            
            switch_idx = None
            for j in range(1,n):
                p = self.track.pieces[(i+j)%n]
                if p.switch:
                    self.next_switch_piece = p
                    self.next_switch_idx = switch_idx
                    switch_idx = (i+j)%n
                    break
            #print("Next switch: %d" % (switch_idx,))
            right_turns = 0
            left_turns = 0
            for j in range(1,n):
                p = self.track.pieces[(switch_idx+j)%n]
                if p.switch:
                    break
                if p.turn:
                    if p.angle > 0:
                        #print("Right at %d" % ((switch_idx+j)%n,))
                        right_turns += 1
                    else: 
                        #print("Left at %d" % ((switch_idx+j)%n,))
                        left_turns += 1
            if right_turns > left_turns and own_pos.start_lane_idx > 0:
                #print("switch right")
                self.next_lane = own_pos.start_lane_idx - 1
                print("next lane: ", self.next_lane)
                self.switch("Right")
                return
            if left_turns > right_turns and own_pos.start_lane_idx < len(self.track.lanes):
                #print("switch left")
                self.next_lane = own_pos.start_lane_idx + 1
                print("next lane: ", self.next_lane)
                self.switch("Left")
                return
            self.next_lane = own_pos.start_lane_idx
            print("next lane: ", self.next_lane)

        # some crude throttle control with magic numbers
        if v == 0:
            self.throttle(1)
        else:
            if own_pos.piece.turn:
                length_of_turn = own_pos.piece.length(own_pos.start_lane_idx) - own_pos.piece_dist
                i = own_pos.piece_idx + 1
                n = len(self.track.pieces)
                while self.track.pieces[i % n].turn:
                    i += 1
                    length_of_turn += self.track.pieces[i % n].length(own_pos.start_lane_idx)
                ticks_of_turn = length_of_turn / v
                drift_estimate = ticks_of_turn * (dt + ddt) + t
                tmp = dt + ddt

                while abs(tmp) > 0.2:
                    tmp *= (1 - DRIFT_DECAY_RATE_EST)
                    drift_estimate += tmp

                print("Turn of %.2f units, drift forecast %.2f" % (length_of_turn, drift_estimate))
                if abs(drift_estimate) >= MAX_DRIFT_ANGLE:
                    self.throttle(0)
                else:
                    self.throttle(1)
            else:
                distance_to_turn = own_pos.piece.length() - own_pos.piece_dist
                i = own_pos.piece_idx + 1
                n = len(self.track.pieces)
                switch_before_corner = False
                while not self.track.pieces[i % n].turn:
                    i += 1
                    if (i % n) == self.next_switch_idx:
                        switch_before_corner = True
                    distance_to_turn += self.track.pieces[i % n].length()
                corner = self.track.pieces[i % n]
                
                corner_lane = own_pos.end_lane_idx if switch_before_corner else self.next_lane
                corner_entry_speed = traction_loss_threshold(self.corner_radius(corner, corner_lane))

                braking_distance = distance_to_target_speed(v, corner_entry_speed)
                print("Turn in %.2f units, braking distance %.2f" % (distance_to_turn, braking_distance))
                if braking_distance >= distance_to_turn:
                    self.throttle(0)
                else:
                    self.throttle(1)

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
        bot = FooBot(s, name, key)
        bot.run(track)
    elif len(sys.argv) != 5:
        print("Usage: ./run host port botname botkey")
    else:
        host, port, name, key = sys.argv[1:5]
        print("Connecting with parameters:")
        print("host={0}, port={1}, bot name={2}, key={3}".format(*sys.argv[1:5]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, int(port)))
        bot = FooBot(s, name, key)
        bot.run()