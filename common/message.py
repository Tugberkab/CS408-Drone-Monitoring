from dataclasses import dataclass, asdict
import json, time, uuid

@dataclass
class SensorReading:
    sensor_id: str
    temperature: float
    humidity: float
    timestamp: str

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode() + b'\n'      # newline-delimited

def iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
