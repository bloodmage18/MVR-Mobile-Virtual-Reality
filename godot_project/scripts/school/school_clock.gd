extends Node
class_name SchoolClock
## The school day from the EZ Weekly Dev Challenge (TimeOfDay.gd + time_table.gd + data.csv),
## running at night. After prep time the timetable enters the DIMENSIONAL period
## (16:00-06:00 in the original), but the bell keeps ringing on the hour anyway.

signal minute_passed(hour: int, minute: int)
signal hour_passed(hour: int, period: String)

@export var hour := 17
@export var minute := 40
@export var seconds_per_minute := 1.5   # 90 real seconds per school hour

# From the challenge's data.csv (Ghanaian SHS-style day)
const TIMETABLE := {
	6: "MORNING DEVOTION",
	7: "ASSEMBLY",
	8: "LESSON 1 AND 2. ENGLISH LANGUAGE",
	9: "SHORT BREAK",
	10: "LESSON 3 AND 4. VISUAL ARTS",
	11: "LESSON 5. CORE MATHEMATICS",
	12: "LUNCH BREAK",
	13: "LESSON 6 AND 7. GENERAL KNOWLEDGE IN ART",
	14: "LESSON 8. SOCIAL STUDIES",
	15: "EXTRA-CURRICULAR ACTIVITIES",
	16: "PREP TIME",
}

var labels: Array[Label3D] = []
var _acc := 0.0


func period(h := -1) -> String:
	return TIMETABLE.get(hour if h < 0 else h, "DIMENSIONAL")


func day_periods() -> Array:
	return TIMETABLE.values()


func time_text() -> String:
	return "%02d:%02d" % [hour, minute]


func _ready() -> void:
	_update_labels()


func _process(delta: float) -> void:
	_acc += delta
	while _acc >= seconds_per_minute:
		_acc -= seconds_per_minute
		_tick()


func _tick() -> void:
	minute += 1
	if minute >= 60:
		minute = 0
		hour = (hour + 1) % 24
		hour_passed.emit(hour, period())
	minute_passed.emit(hour, minute)
	_update_labels()


func _update_labels() -> void:
	for l in labels:
		if is_instance_valid(l):
			l.text = time_text()
