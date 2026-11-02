"""The pipeline wiring: who listens to what, and that every stage is fed. No database. Names WR*."""
from orchestrator.wiring import EXTERNAL_SOURCES, PIPELINE, register_pipeline
from shared.bus.events import CONSUMER_NAME_RE, KNOWN_EVENT_TYPES, EventType


class Worker:
    def __init__(self):
        self.subs = []

    def subscribe(self, etype, name, handler):
        self.subs.append((etype, name, handler))


class Stage:
    def __init__(self, etype, name):
        self.etype, self.name = etype, name

    def register(self, worker):
        worker.subscribe(self.etype, self.name, self.handle)

    def handle(self, conn, event):
        return None


def test_WR1_registering_the_pipeline_subscribes_each_stage_to_its_event():
    w = Worker()
    stages = {"qualifier": Stage("signal.captured", "qualifier"), "drafter": Stage("lead.qualified", "drafter"),
              "guard_consumer": Stage("action.proposed", "compliance_guard"), "closer": Stage("action.approved", "closer")}
    register_pipeline(w, **stages)
    assert [(e, n) for e, n, _ in w.subs] == [("signal.captured", "qualifier"), ("lead.qualified", "drafter"), ("action.proposed", "compliance_guard"),
                                              ("action.approved", "closer")]


def test_WR2_the_chain_is_connected_every_event_is_known_and_every_stage_is_fed():
    produced = {e for _, _, outs in PIPELINE for e in outs}
    assert all(c in KNOWN_EVENT_TYPES and all(o in KNOWN_EVENT_TYPES for o in outs) for c, _, outs in PIPELINE)
    for consumes, name, _ in PIPELINE:
        assert consumes in produced or consumes in EXTERNAL_SOURCES, f"{name} would never be triggered"
    assert EXTERNAL_SOURCES == {"signal.captured", "action.approved"} and len({n for _, n, _ in PIPELINE}) == len(PIPELINE)
    assert all(CONSUMER_NAME_RE.match(n) for _, n, _ in PIPELINE)


def test_WR3_the_real_consumers_use_exactly_the_names_and_events_the_chain_declares():
    from orchestrator import closer, drafter, guard_consumer, qualifier
    declared = {(c, n) for c, n, _ in PIPELINE}
    assert declared == {(EventType.SIGNAL_CAPTURED, qualifier.CONSUMER), (EventType.LEAD_QUALIFIED, drafter.CONSUMER),
                        (EventType.ACTION_PROPOSED, guard_consumer.CONSUMER), (EventType.ACTION_APPROVED, closer.CONSUMER)}
