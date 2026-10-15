"""Fixed source-family identities; layout alone does not prove hook support."""

DECOMPOSED_GATEWAY_TARGETS = (
    "gateway/run_turn.py", "gateway/run_turn_runner.py", "gateway/run_inbound.py",
    "gateway/run_busy.py", "gateway/run_startup.py", "gateway/run_notifications.py",
)
# Hermes v0.22.1+ (297552a9d6) split turn mixins. Optional: earlier layouts do
# not have these files, and they are owned only when present.
DECOMPOSED_SPLIT_TURN_TARGETS = (
    "gateway/run_turn_persistence.py", "gateway/run_turn_progress.py",
)
DECOMPOSED_TARGETS = (*DECOMPOSED_GATEWAY_TARGETS,
                      "cron/scheduler_delivery.py", "gateway/platforms/base.py")


DECOMPOSED_MANIFEST_VERSION = 4
DECOMPOSED_LAYOUT = "gateway-decomposed-v1"
MANIFEST_NAME = ".hermes_feishu_card_manifest"
BACKUP_SUFFIX = ".hermes_feishu_card.bak"
DECOMPOSED_SOURCE_TARGETS = (
    "gateway/run.py", *DECOMPOSED_GATEWAY_TARGETS, *DECOMPOSED_SPLIT_TURN_TARGETS,
    "cron/scheduler.py", "cron/scheduler_delivery.py", "gateway/platforms/base.py",
)
