"""Add one frame endpoint after ESPHome's standard entities, before its context.

Uses the nRF52 endpoint helpers shipped with ESPHome 2026.9.0.
"""
import esphome.codegen as cg
from esphome.core import CORE, CoroPriority, coroutine_with_priority
from esphome.components.zigbee.zigbee_zephyr import (
    ZigbeeClusterDesc,
    get_slot_index,
    zigbee_new_cluster_list,
    zigbee_register_ep,
)


@coroutine_with_priority(CoroPriority.LATE + 1)
async def add_frame_endpoint(display, config):
    # NCS restores the persisted RX-on-when-idle bit after ESPHome setup.
    # Reapply the requested mode at SKIP_STARTUP, before BDB commissioning.
    cg.add_define("GTAG_ZIGBEE_SLEEPY", CORE.config["zigbee"]["sleepy"])
    cg.add_build_flag("-Wl,--wrap=zigbee_default_signal_handler")
    # Lower priority than normal sensor/number generation preserves their IDs.
    slot = get_slot_index()
    CORE.add_global(cg.RawExpression("""
        #define ZB_ZCL_CLUSTER_ID_GTAG_FRAME 0xFC11
        static void gtag_frame_cluster_init() {}
        #define ZB_ZCL_CLUSTER_ID_GTAG_FRAME_SERVER_ROLE_INIT gtag_frame_cluster_init
        #define ZB_ZCL_CLUSTER_ID_GTAG_FRAME_CLIENT_ROLE_INIT gtag_frame_cluster_init
        static zb_uint16_t gtag_frame_revision = 1;
        static zb_zcl_attr_t gtag_frame_attributes[] = {{
          ZB_ZCL_ATTR_GLOBAL_CLUSTER_REVISION_ID, ZB_ZCL_ATTR_TYPE_U16,
          ZB_ZCL_ATTR_ACCESS_READ_ONLY, ZB_ZCL_MANUF_CODE_INVALID,
          &gtag_frame_revision
        }};
    """))
    name, clusters = zigbee_new_cluster_list(
        "gtag_frame_clusters", [ZigbeeClusterDesc("ZB_ZCL_CLUSTER_ID_GTAG_FRAME", "gtag_frame_attributes")]
    )
    zigbee_register_ep("gtag_frame_endpoint", name, 1, clusters, slot, "ZB_HA_CUSTOM_ATTR_DEVICE_ID")
    var = cg.new_Pvariable(config["zigbee_transport_id"])
    await cg.register_component(var, {})
    cg.add(var.set_display(display))
    cg.add(var.set_endpoint(slot + 1))
    cg.add(var.set_parent(await cg.get_variable(config["zigbee_id"])))
