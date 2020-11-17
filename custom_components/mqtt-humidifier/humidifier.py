"""MQTT Humidifier integration"""
import json

from . import DOMAIN

import voluptuous as vol

import homeassistant.helpers.config_validation as cv

from homeassistant.components import humidifier, mqtt
from homeassistant.components.humidifier import HumidifierEntity, ATTR_HUMIDITY, ATTR_AVAILABLE_MODES, ATTR_MODE
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.components.mqtt.discovery import MQTT_DISCOVERY_NEW, clear_discovery_hash
from homeassistant.core import callback
from homeassistant.components.mqtt.debug_info import log_messages

from homeassistant.const import (
    CONF_DEVICE,
    CONF_NAME,
    CONF_OPTIMISTIC,
    CONF_PAYLOAD_OFF,
    CONF_PAYLOAD_ON,
    CONF_STATE,
    CONF_UNIQUE_ID,
)

from homeassistant.components.mqtt.const import (
    ATTR_DISCOVERY_HASH,
    CONF_QOS,
    CONF_RETAIN,
    CONF_STATE_TOPIC,
)
from homeassistant.components.mqtt import (
    CONF_COMMAND_TOPIC,
    PLATFORMS,
    MqttAttributes,
    MqttAvailability,
    MqttDiscoveryUpdate,
    MqttEntityDeviceInfo,
    subscription,
)

DEFAULT_NAME = "MQTT Humidifier"
DEFAULT_PAYLOAD_ON = "ON"
DEFAULT_PAYLOAD_OFF = "OFF"
DEFAULT_OPTIMISTIC = False

CONF_STATE_VALUE_TEMPLATE = "state_value_template"

PLATFORM_SCHEMA = (
    mqtt.MQTT_RW_PLATFORM_SCHEMA.extend(
        {
            vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
            vol.Optional(CONF_OPTIMISTIC, default=DEFAULT_OPTIMISTIC): cv.boolean,
            vol.Optional(CONF_PAYLOAD_OFF, default=DEFAULT_PAYLOAD_OFF): cv.string,
            vol.Optional(CONF_PAYLOAD_ON, default=DEFAULT_PAYLOAD_ON): cv.string,
            vol.Optional(CONF_STATE_VALUE_TEMPLATE): cv.template,
        }
    )
        .extend(mqtt.MQTT_AVAILABILITY_SCHEMA.schema)
        .extend(mqtt.MQTT_JSON_ATTRS_SCHEMA.schema)
)


async def async_setup_platform(
        hass: HomeAssistantType, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up MQTT fan through configuration.yaml."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    await _async_setup_entity(hass, config, async_add_entities)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MQTT fan dynamically through MQTT discovery."""

    async def async_discover(discovery_payload):
        """Discover and add a MQTT fan."""
        discovery_data = discovery_payload.discovery_data
        try:
            config = PLATFORM_SCHEMA(discovery_payload)
            await _async_setup_entity(
                hass, config, async_add_entities, config_entry, discovery_data
            )
        except Exception:
            clear_discovery_hash(hass, discovery_data[ATTR_DISCOVERY_HASH])
            raise

    async_dispatcher_connect(
        hass, MQTT_DISCOVERY_NEW.format(humidifier.DOMAIN, "mqtt"), async_discover
    )


async def _async_setup_entity(
        hass, config, async_add_entities, config_entry=None, discovery_data=None
):
    """Set up the MQTT fan."""
    async_add_entities([MqttHumidifier(hass, config, config_entry, discovery_data)])


class MqttHumidifier(
    MqttAttributes,
    MqttAvailability,
    MqttDiscoveryUpdate,
    MqttEntityDeviceInfo,
    HumidifierEntity,
):
    """A MQTT fan component."""

    def __init__(self, hass, config, config_entry, discovery_data):
        """Initialize the MQTT fan."""
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._state = False
        self._humidity = None
        self._sub_state = None

        self._topic = None
        self._payload = None
        self._templates = None

        # Load config
        self._setup_from_config(config)

        device_config = config.get(CONF_DEVICE)

        MqttAttributes.__init__(self, config)
        MqttAvailability.__init__(self, config)
        MqttDiscoveryUpdate.__init__(self, discovery_data, self.discovery_update)
        MqttEntityDeviceInfo.__init__(self, device_config, config_entry)

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""
        await super().async_added_to_hass()
        await self._subscribe_topics()

    async def discovery_update(self, discovery_payload):
        """Handle updated discovery message."""
        config = PLATFORM_SCHEMA(discovery_payload)
        self._setup_from_config(config)
        await self.attributes_discovery_update(config)
        await self.availability_discovery_update(config)
        await self.device_info_discovery_update(config)
        await self._subscribe_topics()
        self.async_write_ha_state()

    def _setup_from_config(self, config):
        """(Re)Setup the entity."""
        self._config = config
        self._topic = {
            key: config.get(key)
            for key in (
                CONF_STATE_TOPIC,
                CONF_COMMAND_TOPIC,
            )
        }

        self._templates = {
            CONF_STATE: config.get(CONF_STATE_VALUE_TEMPLATE),
        }
        self._payload = {
            "STATE_ON": config[CONF_PAYLOAD_ON],
            "STATE_OFF": config[CONF_PAYLOAD_OFF],
        }

        self._supported_features = 0

        for key, tpl in list(self._templates.items()):
            if tpl is None:
                self._templates[key] = lambda value: value
            else:
                tpl.hass = self.hass
                self._templates[key] = tpl.async_render_with_possible_json_value

    async def _subscribe_topics(self):
        """(Re)Subscribe to topics."""
        topics = {}

        @callback
        @log_messages(self.hass, self.entity_id)
        def state_received(msg):
            """Handle new received MQTT message."""
            payload = self._templates[CONF_STATE](msg.payload)

            payload_dict = json.loads(payload)

            if "state" in payload_dict.keys():
                if payload_dict["state"] == self._payload["STATE_ON"]:
                    self._state = True
                elif payload_dict["state"] == self._payload["STATE_OFF"]:
                    self._state = False
            if "humidity" in payload_dict.keys():
                self._humidity = payload_dict["humidity"]

            self.async_write_ha_state()

        if self._topic[CONF_STATE_TOPIC] is not None:
            topics[CONF_STATE_TOPIC] = {
                "topic": self._topic[CONF_STATE_TOPIC],
                "msg_callback": state_received,
                "qos": self._config[CONF_QOS],
            }

        @callback
        @log_messages(self.hass, self.entity_id)
        def humidity_received(msg):
            """Handle new received MQTT message for the speed."""
            payload = self._templates[ATTR_HUMIDITY](msg.payload)
            self._humidity = payload
            self.async_write_ha_state()

        self._sub_state = await subscription.async_subscribe_topics(
            self.hass, self._sub_state, topics
        )

    async def async_will_remove_from_hass(self):
        """Unsubscribe when removed."""
        self._sub_state = await subscription.async_unsubscribe_topics(
            self.hass, self._sub_state
        )
        await MqttAttributes.async_will_remove_from_hass(self)
        await MqttAvailability.async_will_remove_from_hass(self)
        await MqttDiscoveryUpdate.async_will_remove_from_hass(self)

    @property
    def should_poll(self):
        """No polling needed for a MQTT fan."""
        return False

    @property
    def is_on(self):
        """Return true if device is on."""
        return self._state

    @property
    def name(self) -> str:
        """Get entity name."""
        return self._config[CONF_NAME]

    @property
    def humidity(self):
        """Return the current speed."""
        return self._humidity

    def turn_on(self, humidity: int = None, **kwargs) -> None:
        """Turn on the entity.

        This method is a coroutine.
        """
        mqtt.async_publish(
            self.hass,
            self._topic[CONF_COMMAND_TOPIC],
            '{ "state": "' + self._payload["STATE_ON"] + '"}',
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
        )
        if humidity:
            self.set_humidity(humidity)

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    def set_humidity(self, humidity: int) -> None:
        mqtt.async_publish(
            self.hass,
            self._topic[CONF_COMMAND_TOPIC],
            '{"humidity": ' + str(humidity) + '}',
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
        )

    @property
    def target_humidity(self):
        """Return the humidity we try to reach."""
        return self._humidity

