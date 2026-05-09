import binascii
from automower_ble.helpers import crc
from enum import IntEnum
import asyncio
import logging
import json
from importlib.resources import files
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bleak import BleakClient

logger = logging.getLogger(__name__)


class ModeOfOperation(IntEnum):
    # ProtocolTypes$IMowerAppMowerMode, used in modeOfOperation: 4586, 1
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-mode
    AUTO = 0
    MANUAL = 1
    HOME = 2  # Mower goes home and parks forever. Week schedule is not used. Cannot be overridden with forced mowing.
    DEMO = 3  # Same as main area, but shorter times. No blade operation
    POI = 4


class MowerState(IntEnum):
    # ProtocolTypes$IMowerAppState, used in mowerState: 4586, 2
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-state
    OFF = 0  # Mower is turned off.
    WAIT_FOR_SAFETYPIN = 1
    STOPPED = 2  # Mower is stopped requires manual action.
    FATAL_ERROR = 3
    PENDING_START = 4
    PAUSED = 5  # Mower has been paused by user.
    IN_OPERATION = 6  # See value in activity for status.
    RESTRICTED = (
        7  # Mower can currently not mow due to week calender, or override park.
    )
    ERROR = 8  # An error has occurred. Check errorCode. Mower requires manual action.


class MowerActivity(IntEnum):
    # ProtocolTypes$IMowerAppActivity, used in mowerActivity: 4586, 3
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-activity
    NONE = 0
    CHARGING = 1  # Mower is charging in station due to low battery.
    GOING_OUT = 2
    MOWING = 3  # Mower is mowing lawn. If in demo mode the blades are not in operation.
    GOING_HOME = 4  # Mower is going home to the charging station.
    PARKED = 5
    STOPPED_IN_GARDEN = 6  # Mower has stopped. Needs manual action to resume


class OverrideAction(IntEnum):
    NONE = 0
    FORCEDPARK = 1
    FORCEDMOW = 2


class ResponseResult(IntEnum):
    OK = 0
    UNKNOWN_ERROR = 1
    INVALID_VALUE = 2
    OUT_OF_RANGE = 3
    NOT_AVAILABLE = 4
    NOT_ALLOWED = 5
    INVALID_GROUP = 6
    INVALID_ID = 7
    DEVICE_BUSY = 8
    INVALID_PIN = 9
    MOWER_BLOCKED = 10


class TaskInformation:
    def __init__(
        self,
        next_start_time,
        duration_in_seconds,
        on_monday,
        on_tuesday,
        on_wednesday,
        on_thursday,
        on_friday,
        on_saturday,
        on_sunday,
    ):
        self.next_start_time = next_start_time
        self.duration_in_seconds = duration_in_seconds
        self.on_monday = on_monday
        self.on_tuesday = on_tuesday
        self.on_wednesday = on_wednesday
        self.on_thursday = on_thursday
        self.on_friday = on_friday
        self.on_saturday = on_saturday
        self.on_sunday = on_sunday


class Command:
    def __init__(self, channel_id: int, parameter: dict):
        self.channel_id = channel_id

        self.major = parameter["major"]
        self.minor = parameter["minor"]

        self.request_data_type = parameter.get("requestType")

        if "responseType" not in parameter:
            parameter["responseType"] = "no_response"

        if not isinstance(parameter["responseType"], dict):  # Always wrap in list
            self.response_data_type = {"response": parameter["responseType"]}
        else:
            self.response_data_type = parameter["responseType"]
        self.request_data = bytearray()

    def generate_request(self, **kwargs) -> bytearray:
        self.request_data = bytearray(18)
        self.request_data[0] = 0x02  # Hard coded value (start of packet)
        self.request_data[1] = 0xFD  # 0xFD = LINKED_PACKET_TYPE
        self.request_data[2] = 0x00  # Length, low byte, updated later
        self.request_data[3] = 0x00  # Length, high byte, updated later

        # ChannelID
        self.request_data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        self.request_data[8] = 0x01  # is_linked (usually 0x01)

        self.request_data[9] = 0x00  # CRC, Updated later
        self.request_data[10] = (
            0x00  # Packet type (0x00 = request, 0x01 = response, 0x02 = event)
        )
        self.request_data[11] = 0xAF  # Hard coded value

        major_bytes = self.major.to_bytes(2, byteorder="little")

        self.request_data[12] = major_bytes[0]  # low byte of 'module'
        self.request_data[13] = major_bytes[1]  # high byte of 'module'
        self.request_data[14] = self.minor  # low byte of 'command'
        self.request_data[15] = 0x00  # high byte of 'command'

        # Byte 16 represents length of request data type
        request_length = 0
        request_data = bytearray()
        if self.request_data_type is not None:
            for request_name, request_type in self.request_data_type.items():
                if request_name not in kwargs:
                    raise ValueError(
                        "Missing request parameter: "
                        + request_name
                        + " for command ("
                        + str(self.major)
                        + ", "
                        + str(self.minor)
                        + ")"
                    )

                if request_type == "uint32":
                    request_length += 4
                    request_data += kwargs[request_name].to_bytes(4, byteorder="little")
                elif request_type == "uint16":
                    request_length += 2
                    request_data += kwargs[request_name].to_bytes(2, byteorder="little")
                elif request_type == "uint8":
                    request_length += 1
                    request_data += kwargs[request_name].to_bytes(1, byteorder="little")
                else:
                    raise ValueError("Unknown request type: " + request_type)
        self.request_data[16] = request_length

        self.request_data[17] = 0x00  # high byte of request_length
        if request_length > 0:
            self.request_data += request_data

        self.request_data[2] = len(self.request_data) - 2  # Length

        self.request_data[9] = crc(self.request_data, 1, 8)  # CRC

        # Two last bytes are crc and 0x03
        self.request_data.append(crc(self.request_data, 1, len(self.request_data) - 1))
        self.request_data.append(0x03)  # Hard coded value

        return self.request_data

    def parse_response(self, response_data: bytearray) -> dict[str, int | str] | None:
        response_length = response_data[17]
        data = response_data[19 : 19 + response_length]
        response: dict[str, int | str] = {}
        dpos = 0  # data position
        for name, dtype in self.response_data_type.items():
            if dtype == "no_response":
                return None
            if (dtype == "tUnixTime") or (dtype == "uint32"):
                response[name] = int.from_bytes(
                    data[dpos : dpos + 4], byteorder="little"
                )
                dpos += 4
            elif dtype == "uint16":
                response[name] = int.from_bytes(
                    data[dpos : dpos + 2], byteorder="little"
                )
                dpos += 2
            elif (dtype == "uint8") or (dtype == "bool"):
                response[name] = data[dpos]
                dpos += 1
            elif dtype == "ascii":
                if len(self.response_data_type) != 1:
                    raise ValueError(
                        "ASCII response type can currently only be used when there is only one response type"
                    )
                response[name] = data.decode("ascii").rstrip(
                    "\x00"
                )  # Remove trailing null bytes
                dpos += len(data)
            else:
                raise ValueError("Unknown data type: " + dtype)
        if dpos != len(data):
            raise ValueError(f"Data length mismatch. Read {dpos} bytes of {len(data)}")
        return response

    def validate_command_response(self, response_data: bytearray) -> bool:
        if response_data[0] != 0x02:
            return False

        if response_data[1] != 0xFD:
            return False

        if response_data[3] != 0x00:  # high byte of length
            return False

        if response_data[4:8] != self.channel_id.to_bytes(4, byteorder="little"):
            return False

        if response_data[8] != 0x01:
            # This is a valid config, but we don't support it
            # return m1656b(decodeState, c10786f);
            return False

        if response_data[9] != crc(response_data, 1, 8):
            return False

        if response_data[10] != 0x01:  # packet type is not 0x01 = response
            return False

        if response_data[11] != 0xAF:
            return False

        major_bytes = self.major.to_bytes(4, byteorder="little")
        if response_data[12] != major_bytes[0]:
            return False
        if response_data[13] != major_bytes[1]:
            return False
        if response_data[14] != self.minor:
            return False

        if response_data[15] != 0x00:  # high byte of 'command' (self.minor)
            return False

        if (
            response_data[16] != 0x00
        ):  # result: OK(0), UNKNOWN_ERROR(1), INVALID_VALUE(2), OUT_OF_RANGE(3), NOT_AVAILABLE(4), NOT_ALLOWED(5), INVALID_GROUP(6), INVALID_ID(7), DEVICE_BUSY(8), INVALID_PIN(9), MOWER_BLOCKED(10);
            logger.warning("Non zero response result: %d", response_data[16])
            return False

        return True


class BLEClient:
    def __init__(
        self,
        channel_id: int,
        address,
        pin=None,
        *,
        pair_on_connect: bool = True,
    ):
        self.channel_id = channel_id
        self.address = address
        self.pin = pin
        self.pair_on_connect = pair_on_connect
        self.MTU_SIZE = 20

        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[bytearray] = asyncio.Queue()

        self.client: BleakClient | None = None
        self.protocol = None

    async def get_protocol(self):
        if self.protocol is None:

            def read_protocol_file():
                with files("automower_ble").joinpath("protocol.json").open("r") as f:
                    return json.load(f)

            self.protocol = await asyncio.get_running_loop().run_in_executor(
                None, read_protocol_file
            )
        return self.protocol

    async def _get_response(self):
        try:
            data = await asyncio.wait_for(self.queue.get(), timeout=10)

        except TimeoutError:
            logger.error("Unable to get response from device: '%s'", self.address)
            return None

        return data

    async def _write_data(self, data):
        logger.info("Writing: %s", str(binascii.hexlify(data)))

        chunk_size = self.MTU_SIZE - 3
        for chunk in (
            data[i : i + chunk_size] for i in range(0, len(data), chunk_size)
        ):
            await self.client.write_gatt_char(self.write_char, chunk, response=False)

        logger.debug("Finished writing")

    async def _read_data(self):
        data = await self._get_response()

        if data is None:
            return None

        if len(data) < 3:
            # We got such a small amount of data, let's try again
            if chunk := await self._get_response() is None:
                return None
            data = data + chunk

            if len(data) < 3:
                # Something is wrong
                return None

        length = data[2] + 4

        logger.debug("Waiting for %d bytes", length)

        while len(data) < length:
            try:
                data = data + await asyncio.wait_for(self.queue.get(), timeout=5)
            except TimeoutError:
                logger.error(
                    "Unable to get full response from device: '%s', currently have %s",
                    str(binascii.hexlify(data)),
                    self.address,
                )
                logger.error("Expecting %d bytes, only have %d", length, len(data))
                return None

        logger.info("Final response: %s", str(binascii.hexlify(data)))

        return data

    async def _request_response(self, request_data):
        async with self.lock:
            try:
                # If there are previous responses, flush them out
                while not self.queue.empty():
                    await self.queue.get()

                await self._write_data(request_data)

                response_data = await self._read_data()
                if response_data is None:
                    logger.error(
                        "Unable to communicate with device: '%s'", self.address
                    )
                    if self.is_connected():
                        await self.disconnect()
                    return None

            except asyncio.exceptions.CancelledError:
                logger.debug("Received CancelledError")
                if self.is_connected():
                    await self.disconnect()
                return None

        return response_data

    async def connect(self, device) -> ResponseResult:
        """
        Connect to a device and setup the channel

        Returns a ResponseResult
        """
        logger.info("starting scan...")

        if device is None:
            logger.error("could not find device with address '%s'", self.address)
            return ResponseResult.UNKNOWN_ERROR

        logger.info("connecting to device...")
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
        )
        logger.info("connected")

        # Best-effort BLE-level pairing. The Husqvarna mowers authenticate at
        # the application protocol layer via EnterOperatorPin, not via SMP.
        # On Linux/BlueZ, client.pair() commonly fails with AuthenticationFailed
        # against mowers that refuse SMP outright (e.g. Sileno Minimo). Worse,
        # those mowers also drop the link as soon as they see an SMP request
        # they don't like, and continue to refuse GATT operations like CCCD
        # writes for several seconds afterwards.
        #
        # Caller can opt out via pair_on_connect=False (or --no-pair on the
        # CLI) — preferred on Linux for mowers that don't need SMP at all.
        # If pair() raises, we tear down the link and reconnect from scratch
        # without calling pair() the second time, after a settling delay.
        if self.pair_on_connect:
            logger.info("pairing device...")
            try:
                await self.client.pair()
                logger.info("paired")
            except BleakError as e:
                logger.warning(
                    "BLE pair() failed (%s); reconnecting without OS-level "
                    "pairing. Tip: pass --no-pair on the CLI (or "
                    "pair_on_connect=False) to skip this step entirely on "
                    "mowers that refuse SMP.",
                    e,
                )
                try:
                    await self.client.disconnect()
                except BleakError as disc_err:
                    logger.debug("disconnect after pair failure: %s", disc_err)
                # Give the mower's BLE stack ~3s to leave its post-rejection
                # penalty state — shorter delays cause the next CCCD write
                # (start_notify) to come back as ATT UNLIKELY_ERROR.
                await asyncio.sleep(3.0)
                self.client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    device.name or "Unknown Device",
                )
                logger.info("reconnected without pair()")
        else:
            logger.info("skipping client.pair() (pair_on_connect=False)")

        # This is not safe, _mtu_size is not defined in BaseBleakClient but may
        # be defined in subclasses.
        self.client._backend._mtu_size = self.MTU_SIZE  # type: ignore[attr-defined]

        # Locate the Husqvarna write/notify characteristics by UUID.
        # The previous version of this loop probed every readable characteristic
        # for diagnostic logging, but on Linux/BlueZ those probe-reads of
        # standard GAP attributes (e.g. 0x2a04 Peripheral Preferred Connection
        # Parameters) frequently return UNLIKELY_ERROR, which then invalidates
        # bleak's service cache so subsequent writes fail with "Service
        # Discovery has not been performed yet". Iterate without reading.
        logger.info("looking up Husqvarna characteristics...")
        self.write_char = None  # type: ignore[assignment]
        self.read_char = None  # type: ignore[assignment]
        husqvarna_service_seen = False
        for service in self.client.services:
            logger.debug("[Service] %s", service)
            if service.uuid == "98bd0001-0b0e-421a-84e5-ddbf75dc6de4":
                husqvarna_service_seen = True
            for char in service.characteristics:
                logger.debug(
                    "  [Characteristic] %s (%s)", char, ",".join(char.properties)
                )
                if char.uuid == "98bd0002-0b0e-421a-84e5-ddbf75dc6de4":
                    self.write_char = char
                elif char.uuid == "98bd0003-0b0e-421a-84e5-ddbf75dc6de4":
                    self.read_char = char

        if self.write_char is None or self.read_char is None:
            logger.error(
                "Husqvarna BLE characteristics not found on '%s' "
                "(Husqvarna service present: %s). On Linux, try: "
                "`bluetoothctl trust %s`",
                self.address,
                husqvarna_service_seen,
                self.address,
            )
            return ResponseResult.NOT_ALLOWED
        logger.info("found write/notify characteristics")

        async def notification_handler(
            characteristic: BleakGATTCharacteristic, data: bytearray
        ):
            logger.info("Received: %s", str(binascii.hexlify(data)))
            await self.queue.put(data)

        # Pre-flight: prove the link can actually carry GATT traffic before we
        # touch the notify CCCD. On HA OS / BlueZ 5.7x we've seen StartNotify
        # hang silently while the link itself is fine. A simple read of the
        # read-only 98bd0004 characteristic is a good liveness probe — it
        # proves the mower is happy with unauthenticated GATT requests.
        try:
            probe_value = await asyncio.wait_for(
                self.client.read_gatt_char(
                    "98bd0004-0b0e-421a-84e5-ddbf75dc6de4"
                ),
                timeout=5.0,
            )
            logger.info(
                "link health probe (read 98bd0004): %s",
                binascii.hexlify(probe_value).decode(),
            )
        except (TimeoutError, BleakError) as e:
            logger.warning(
                "link health probe failed (%s); proceeding to start_notify "
                "anyway — the mower may still accept the CCCD write",
                e,
            )

        logger.info("subscribing to notifications...")
        try:
            await asyncio.wait_for(
                self.client.start_notify(self.read_char, notification_handler),
                timeout=15.0,
            )
            logger.info("subscribed to notifications via start_notify")
        except (TimeoutError, BleakError) as e:
            logger.warning(
                "start_notify on %s did not complete (%s); falling back to "
                "direct CCCD write",
                self.read_char.uuid,
                e,
            )
            cccd_handle = None
            for desc in self.read_char.descriptors:
                if desc.uuid == "00002902-0000-1000-8000-00805f9b34fb":
                    cccd_handle = desc.handle
                    break
            if cccd_handle is None:
                logger.error(
                    "no CCCD descriptor found on %s — cannot enable "
                    "notifications by any path",
                    self.read_char.uuid,
                )
                return ResponseResult.UNKNOWN_ERROR
            try:
                await asyncio.wait_for(
                    self.client.write_gatt_descriptor(
                        cccd_handle, bytes([0x01, 0x00])
                    ),
                    timeout=5.0,
                )
                logger.info(
                    "CCCD direct write succeeded; registering notification "
                    "handler manually"
                )
                # Register the handler against bleak's existing notification
                # plumbing so PropertiesChanged signals are routed to our
                # queue. start_notify normally does both the CCCD write and
                # the registration; we did the write ourselves so we just
                # need to attach the callback. _notification_callbacks lives
                # on the BlueZ backend for this purpose.
                backend = self.client._backend
                callbacks = getattr(backend, "_notification_callbacks", None)
                if callbacks is not None:
                    callbacks[self.read_char.handle] = notification_handler  # type: ignore[index]
                else:
                    logger.error(
                        "bleak backend has no _notification_callbacks dict; "
                        "this version of bleak isn't supported by the "
                        "fallback path"
                    )
                    return ResponseResult.UNKNOWN_ERROR
            except (TimeoutError, BleakError) as inner:
                logger.error(
                    "CCCD direct write also failed: %s. The mower is "
                    "silently ignoring writes to handle %s — likely a "
                    "BlueZ-on-HA-OS encryption-promotion issue we can't "
                    "fix from Python.",
                    inner,
                    cccd_handle,
                )
                return ResponseResult.UNKNOWN_ERROR

        # Brief settling delay before the first write. The original library
        # used 5s here; 1s is enough on every device tested so far and keeps
        # the user-facing wait short.
        await asyncio.sleep(1.0)

        logger.info("sending channel-id setup...")
        request = self.generate_request_setup_channel_id()
        response = await self._request_response(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        request = self.generate_request_handshake()
        response = await self._request_response(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        # Warmup sequence required by some mower firmwares (notably the
        # Gardena Sileno Minimo line) before they will accept EnterOperatorPin.
        # The official Husqvarna app sends GetModel, KeepAlive and
        # SetObstacleAvoidanceEnabled(0) immediately after the handshake.
        # SetObstacleAvoidanceEnabled is rejected with INVALID_ID on mowers
        # without the feature, but the firmware still treats issuing it as
        # part of opening the session — skipping it leaves the session in a
        # state where PIN entry silently fails.
        await self._run_warmup()

        if self.pin is not None:
            command = Command(
                self.channel_id, (await self.get_protocol())["EnterOperatorPin"]
            )
            request = command.generate_request(code=self.pin)
            response = await self._request_response(request)
            if response is None:
                return ResponseResult.UNKNOWN_ERROR
            result = self.get_response_result(response)
            # If the result is UNKNOWN_ERROR, assume the pin was invalid
            return (
                ResponseResult.INVALID_PIN
                if result == ResponseResult.UNKNOWN_ERROR
                else result
            )

        return ResponseResult.OK

    async def _run_warmup(self) -> None:
        """Issue the post-handshake sequence the official app sends.

        Best-effort: each call's failure is logged but does not abort the
        connection. We need the *attempts* on the wire — their success is
        not what unlocks the session.
        """
        protocol = await self.get_protocol()
        warmup: list[tuple[str, dict]] = [
            ("GetModel", {}),
            ("KeepAlive", {}),
            ("SetObstacleAvoidanceEnabled", {"enabled": 0}),
        ]
        for name, kwargs in warmup:
            try:
                command = Command(self.channel_id, protocol[name])
                request = command.generate_request(**kwargs)
                response = await self._request_response(request)
                if response is None:
                    logger.debug("Warmup %s: no response", name)
            except Exception as e:
                logger.debug("Warmup %s failed: %s", name, e)

    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def probe_gatts(self, device):
        logger.info("connecting to device...")
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
            max_attempts=3,  # Will retry up to 3 times with backoff
        )
        logger.info("connected")

        manufacture = None
        model = None
        device_type = None

        for service in client.services:
            logger.debug("[Service] %s", service)

            if service.uuid == "98bd0001-0b0e-421a-84e5-ddbf75dc6de4":
                manufacture = service.description

            for char in service.characteristics:
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        logger.debug(
                            "  [Characteristic] %s (%s), Value: %r",
                            char,
                            ",".join(char.properties),
                            value,
                        )
                    except Exception as e:
                        logger.error(
                            "  [Characteristic] %s (%s), Error: %s",
                            char,
                            ",".join(char.properties),
                            e,
                        )

                else:
                    logger.debug(
                        "  [Characteristic] %s (%s)", char, ",".join(char.properties)
                    )

                if char.uuid == "00002a00-0000-1000-8000-00805f9b34fb":
                    model = (await client.read_gatt_char(char)).decode()

                if char.uuid == "98bd0004-0b0e-421a-84e5-ddbf75dc6de4":
                    device_type = (
                        (await client.read_gatt_char(char)).rstrip(b"\x00").decode()
                    )

        await client.disconnect()

        return (manufacture, device_type, model)

    async def disconnect(self):
        """
        Disconnect from the mower, this should be called after every
        `connect()` before the Python script exits
        """

        await self.client.stop_notify(self.read_char)
        await self.queue.put(None)

        logger.info("disconnecting...")
        await self.client.disconnect()
        logger.info("disconnected")

    def generate_request_setup_channel_id(self) -> bytearray:
        """
        Setup the channelID with an Automower, this is the first
        command that should be sent
        """
        data = bytearray.fromhex("02fd160000000000002e1400000000000000004d61696e00")

        # New ChannelID
        data[11:15] = self.channel_id.to_bytes(4, byteorder="little")

        # CRC and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def generate_request_handshake(self) -> bytearray:
        """
        Generate a request handshake. This should be called after
        the channel id is set up but before other commands
        """
        data = bytearray.fromhex("02fd0a000000000000d00801")

        data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        # CRCs and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def validate_response(self, response_data: bytearray) -> bool:
        if response_data[0] != 0x02:
            return False

        if response_data[1] != 0xFD:
            return False

        if response_data[3] != 0x00:  # high byte of length
            return False

        if response_data[4:8] != self.channel_id.to_bytes(4, byteorder="little"):
            return False

        if response_data[8] != 0x01:
            # This is a valid config, but we don't support it
            # return m1656b(decodeState, c10786f);
            return False

        if response_data[9] != crc(response_data, 1, 8):
            return False

        if response_data[10] != 0x01:  # packet type is not 0x01 = response
            return False

        if response_data[11] != 0xAF:
            return False

        if (
            response_data[16] != 0x00
        ):  # result: OK(0), UNKNOWN_ERROR(1), INVALID_VALUE(2), OUT_OF_RANGE(3), NOT_AVAILABLE(4), NOT_ALLOWED(5), INVALID_GROUP(6), INVALID_ID(7), DEVICE_BUSY(8), INVALID_PIN(9), MOWER_BLOCKED(10);
            logger.warning("Non zero response result: %d", response_data[16])
            return False

        return True

    def get_response_result(self, response_data: bytearray) -> ResponseResult:
        if self.validate_response(response_data) is False:
            # Just log if the response is invalid as this has been seen with user
            # logs from official apps. I.e. it is somewhat expected.
            logger.warning("Response failed validation")

        return ResponseResult(response_data[16])
