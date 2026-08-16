# Compiled protobuf definitions for the Meshtastic MQTT panel (see
# meshtastic_mqtt.py). mesh_min.proto/mqtt_min.proto are a hand-trimmed
# subset of meshtastic/protobufs' mesh.proto/mqtt.proto — same field
# numbers/types as upstream (wire-compatible), just missing the ~90% of
# fields HenWen doesn't read. portnums.proto is vendored unmodified (it's
# small and self-contained, no imports).
#
# To recompile after editing a .proto file:
#   protoc --proto_path=/opt/HenWen --python_out=/opt/HenWen \
#     meshtastic_proto/mesh_min.proto meshtastic_proto/mqtt_min.proto \
#     meshtastic_proto/portnums.proto
