import json
import socket
import sys
import math
from time import clock

MAX_DRIFT_ANGLE = 60
DECELERATION_RATE = 0.02

TRACTION_CALIBRATED = False
TRACTION_EST = 0.321
CALIBRATION_THROTTLE = 0.2
CORNER_MODIFIERS = None

DRIFT_DECAY_RATE_EST = 0.2
LOG = True

def sign(x):
    return -1 if x < 0 else 1

def log(s):
    if LOG: print(s)

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
    max_abs_theta = 0

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
        self.lap = piece_pos['lap']
        self.piece_idx = piece_pos['pieceIndex']
        self.piece = track.pieces[self.piece_idx]
        self.piece_dist = piece_pos['inPieceDistance']
        self.start_lane_idx = piece_pos['lane']['startLaneIndex']
        self.end_lane_idx = piece_pos['lane']['endLaneIndex']

    def __repr__(self):
        return "%d/%d on piece %d, (%d-%d), at angle %.2f" % (self.piece_dist, self.piece.length(self.start_lane_idx), self.piece_idx, self.start_lane_idx, self.end_lane_idx, self.angle)

class FooBot(object):
    x, v, dv, t, dt, ddt = 0, 0, 0, 0, 0, 0
    xs, dxs, vs, ts, dts, ddts = [0], [0], [0], [0], [0], [0]
    crashed = False

    def __init__(self, socket, name, key):
        self.socket = socket
        self.name = name
        self.key = key
        self.ticks = 0
        self.next_switch_piece = None
        self.next_switch_idx = -1
        self.next_lane = -1
        self.positions = []
        self.can_turbo = False
        self.turbo_piece_index = -1
        self.pos = None
        self.last_pos = None

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
            return self.msg("joinRace", data)

    def throttle(self, throttle):
        x = int(round(throttle / 0.1))
        vis = "["+ '='*x + ' '*(10-x) +"]"
        log(vis)
        self.msg("throttle", throttle)

    def activate_turbo(self):
        log("============ activating turbo ============")
        self.msg("turbo", "wheeeeeee")

    def switch(self, direction):
        self.msg("switchLane", direction)

    def ping(self):
        log('.. ping ..')
        self.msg("ping", {})

    def on_game_init(self, data):
        global CORNER_MODIFIERS
        race = data['race']
        self.track = Track(race['track'])
        log(race['raceSession'])
        self.session = Session(race['raceSession'])
        log(self.track)
        log(self.session)
        self.cars = race['cars']
        self.guide_flag_pos = self.cars[0]['dimensions']['guideFlagPosition']
        self.length = self.cars[0]['dimensions']['length']

        # find start of longest straight for turbo action
        n = len(self.track.pieces)
        turbo_spot = -1
        max_straight_length = 0
        for i in range(n):
            if self.track.pieces[i].turn and not self.track.pieces[(i + 1) % n].turn:
                j = 1
                while not self.track.pieces[(i + 1 + j) % n].turn:
                    j += 1
                if j > max_straight_length:
                    max_straight_length = j
                    turbo_spot = (i + 1) % n
        self.turbo_piece_index = turbo_spot

        if CORNER_MODIFIERS == None or len(CORNER_MODIFIERS) != len(self.track.pieces):
            CORNER_MODIFIERS = [1]*len(self.track.pieces)

    def corner_radius(self, piece, lane):
        turn_radius = 0 if not piece.turn else piece.radius
        if turn_radius > 0:
            if piece.angle > 0:
                turn_radius -= self.track.lanes[lane].offset
            else:
                turn_radius += self.track.lanes[lane].offset
        return turn_radius

    def update(self, data):
        cars = dict()
        for json in data:
            cars[json['id']['color']] = Position(self.track, json)
        
        self.pos = cars[self.color]
        self.last_pos = None

        self.t = self.pos.angle
        self.x = math.sin(self.t / 180.0 * math.pi) * (self.length - self.guide_flag_pos)
        self.dx = self.x - self.xs[-1]

        if len(self.positions) > 0:
            self.last_pos = self.positions[-1]
            self.dt = self.t - self.last_pos.angle
            if self.last_pos.piece_idx == self.pos.piece_idx:
                self.v = self.pos.piece_dist - self.last_pos.piece_dist
            else:
                start_idx = self.last_pos.start_lane_idx
                end_idx = self.last_pos.end_lane_idx
                # lets not try figure out lane change track lengths...
                if start_idx != end_idx:
                    self.v = self.vs[-1]
                else:
                    self.v = self.last_pos.piece.length(start_idx) - self.last_pos.piece_dist + self.pos.piece_dist

        self.dv = self.v - self.vs[-1]
        self.ddt = self.dt - self.dts[-1]

        log("lap %d piece %d, %d/%d, v = %.2f, dv = %.2f, t = %.2f, dt= %.2f" % \
            (self.pos.lap, self.pos.piece_idx, self.pos.piece_dist, \
             self.pos.piece.length(self.pos.start_lane_idx), \
             self.v, self.dv, self.t, self.dt))

    def turbo_logic(self):
        if self.pos.piece_idx == self.turbo_piece_index and self.can_turbo:
            self.can_turbo = False
            self.activate_turbo()
            return True
        return False

    def switch_logic(self):
        if self.next_switch_piece in [None, self.pos.piece] and self.pos.piece_dist > (self.pos.piece.length() / 2):
            i = self.pos.piece_idx
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
            own_lane = self.pos.end_lane_idx
            if right_turns > left_turns and own_lane < len(self.track.lanes) - 1:
                self.next_lane = own_lane + 1
                log("switch right at piece %d, next lane: %d" % (switch_idx, self.next_lane))
                self.switch("Right")
                return True
            if left_turns > right_turns and own_lane > 0:
                self.next_lane = own_lane - 1
                log("switch left at piece %d, next lane: %d" % (switch_idx, self.next_lane))
                self.switch("Left")
                return True
            self.next_lane = own_lane
            log("keep lane %d at piece %d" % (self.next_lane, switch_idx))
        return False

    def calibration_logic(self):
        global TRACTION_CALIBRATED
        global TRACTION_EST
        global CALIBRATION_THROTTLE

        if not TRACTION_CALIBRATED:
            if self.pos.piece.turn:
                log("speed %.2f" % (self.v,))
                if self.t == 0:                
                    CALIBRATION_THROTTLE = 1
                else:
                    TRACTION_EST = (self.vs[-1]**2) / self.pos.piece.radius * 0.9
                    log("------CALIBRATED------- c = %.2f" % (TRACTION_EST,))
                    TRACTION_CALIBRATED = True
                    return False
                log("throttle %.2f theta %.4f" % (CALIBRATION_THROTTLE, self.t))
                self.throttle(1)
                return True
            else:
                lane = self.pos.end_lane_idx
                distance_to_next = self.pos.piece.length(lane) - self.pos.piece_dist
                i = self.pos.piece_idx + 1
                n = len(self.track.pieces)
                for o in range(1,10):
                    i = (self.pos.piece_idx + o) % n
                    pc = self.track.pieces[i]
                    if pc.turn:
                        corner_entry_speed = CALIBRATION_THROTTLE*10
                        braking_distance = distance_to_target_speed(self.v + self.dv, corner_entry_speed)
                        if braking_distance >= distance_to_next:
                            log("Turn %d in %.2f units, braking distance %.2f" % (i, distance_to_next, braking_distance))
                            self.throttle(0)
                            return True
                    distance_to_next += pc.length(lane)
                self.throttle(1)
                return True    
        return False

    def speed_logic(self):
        global CORNER_MODIFIERS
        lane = self.pos.end_lane_idx
        distance_to_next = self.pos.piece.length(lane) - self.pos.piece_dist

        i = self.pos.piece_idx + 1
        n = len(self.track.pieces)
        
        last_radius = self.pos.piece.radius if self.pos.piece.turn else float('inf')
        for o in range(1,10):
            i = (self.pos.piece_idx + o) % n
            pc = self.track.pieces[i]
            pc_radius = pc.radius if pc.turn else float('inf')
            if pc.turn:
                corner_entry_speed = traction_loss_threshold(self.corner_radius(pc, lane)) * CORNER_MODIFIERS[i]
                braking_distance = distance_to_target_speed(self.v + self.dv, corner_entry_speed)
                if braking_distance >= distance_to_next:
                    log("Turn in %.2f units, braking distance %.2f" % (distance_to_next, braking_distance))
                    self.throttle(0)
                    return True
                distance_to_next += pc.length(lane)
            if pc.switch:
                lane = self.next_lane
            last_radius = pc_radius      

        if not self.pos.piece.turn:
            self.throttle(1)
            return True

        return False


    def drift_logic(self):
        global CORNER_MODIFIERS
        if self.pos.piece.turn:
            corner = self.pos.piece            
            i = self.pos.piece_idx
            corner_lane = self.pos.end_lane_idx
            target_speed = traction_loss_threshold(self.corner_radius(corner, corner_lane)) * CORNER_MODIFIERS[i]
            self.throttle(min(1, target_speed / 10))
            return True
        return False

    def on_car_positions(self, data):
        self.update(data)

        if not self.crashed:
            if self.calibration_logic():
                pass
            elif self.turbo_logic(): 
                pass
            elif self.switch_logic(): 
                pass
            elif self.v == 0:
                self.throttle(1)
            elif self.speed_logic():
                pass
            elif self.drift_logic():
                pass
            else: 
                self.ping()
        else:
            self.ping()

        self.pos.piece.max_abs_theta = max(abs(self.t), self.pos.piece.max_abs_theta)
        self.positions.append(self.pos)
        self.vs.append(self.v)
        self.xs.append(self.x)
        self.dxs.append(self.dx)
        self.ts.append(self.t)
        self.ticks += 1

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
            'lapFinished': self.on_lap_finished,
            'turboAvailable': self.on_turbo_enable
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
            if msg_type == 'tournamentEnd':
                break
            after = clock()
            line = socket_file.readline()
            times.append(after - before)
        print("time pondering:")
        print("min", min(times))
        print("max", max(times))
        print("avg", sum(times) / len(times))

    def on_game_start(self, data):
        log("Race started")
        self.crashed = False
        self.can_turbo = False
        self.ping()

    def on_crash(self, data):
        global CORNER_MODIFIERS
        if data['color'] == self.color:
            log("I crashed")
            i = self.pos.piece_idx
            for j in range(3):
                CORNER_MODIFIERS[i - j] *= 0.9
            self.crashed = True
        else:
            log("Someone crashed")
        self.ping()

    def on_spawn(self, data):
        if data['color'] == self.color:
            log("I spawned")
            self.crashed = False
        else:
            log("Someone spawned")
        self.ping()

    def on_game_end(self, data):
        log("Race ended")
        self.ping()

    def on_error(self, data):
        log("Error: {0}".format(data))
        self.ping()

    def on_lap_finished(self, data):
        global CORNER_MODIFIERS
        c = data['car']['color']
        t = data['lapTime']['millis']
        log("======= %s: %d ms =======" % (c, t))

        if c == self.color:
            n = len(self.track.pieces)                 
            for i in range(n):
                piece_i = self.track.pieces[i]
                if piece_i.turn: 
                    current_max = 0
                    for l in range(3):
                        lookahead_pc = self.track.pieces[(i + l) % n]
                        current_max = max(abs(lookahead_pc.max_abs_theta), current_max)
                    if current_max < 10:
                        CORNER_MODIFIERS[i] *= 1.1
                    elif current_max < 0.9 * MAX_DRIFT_ANGLE: # leave 10% margin
                        CORNER_MODIFIERS[i] *= 1 - math.log(current_max / (MAX_DRIFT_ANGLE * 0.9)) / 16
                    log("modifier for piece %d: %.2fx\tmax theta %.1f" % (i, CORNER_MODIFIERS[i], current_max))

        self.ping()

    def on_turbo_enable(self, data):
        if not self.crashed:
            log("---------  turbo available  ---------")
            self.can_turbo = True
        else:
            log("--------- cannot into turbo ---------")
        self.ping()

    def on_join(self, data):
        log("Joined")
        self.ping()

    def on_car_id(self, data):
        color = data['color']
        log("Identified as " + color)
        self.color = color

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