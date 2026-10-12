"""
DAVE (Discord Audio & Video End-to-End Encryption) session glue.

The MLS half (groups, key packages, proposals, commits, welcomes, media
frame encryption) is handled by the `davey` package. This module owns the
voice-gateway opcode flow around it: https://daveprotocol.com

`on_*` methods are called by the voice websocket handler with payloads
already stripped of sequence numbers and opcode headers.
"""

import asyncio
import logging
import threading

import davey

logger = logging.getLogger("dave")

MLS_KEY_PACKAGE = 26
MLS_COMMIT_WELCOME = 28
DAVE_TRANSITION_READY = 23
DAVE_INVALID_COMMIT_WELCOME = 31


class DaveSessionManager:
    def __init__(self, user_id: int, channel_id: int, send_json, send_binary):
        """
        `send_json`/`send_binary` are callables (op, data) / (op, bytes)
        that schedule a websocket send; they need not be awaited.
        """
        self.user_id = user_id
        self.channel_id = channel_id
        self.send_json = send_json
        self.send_binary = send_binary

        self.protocol_version = 0
        self.session: davey.DaveSession | None = None
        self.pending_transitions: dict[int, int] = {}
        self.downgraded = False
        self.connected_user_ids: set[int] = set()

        self.reinitializing = False
        self.last_transition_id: int | None = None
        self.ready_event = asyncio.Event()
        self._external_sender: bytes | None = None  # may arrive before op 4
        # davey's session is a Rust object, mutated in place by transitions on
        # the voice websocket thread while the video and audio sender threads
        # encrypt on it. Every call into it takes this lock: two threads inside
        # one Rust object is a segfault, and it surfaces wherever the corrupted
        # heap happens to be touched next, not at the call site. Reentrant,
        # because the mutators nest (a commit can recover, which reinits).
        self._lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return self.session_or_none is not None

    @property
    def session_or_none(self) -> "davey.DaveSession | None":
        """The session, but only once it is usable.

        `session` exists as soon as the protocol version is known, while
        every method on it needs the group to be established. Collapsing
        both facts into one value means call sites do not have to
        re-derive the condition to convince a type checker (or a reader)
        that the session can actually be used.
        """
        if self.protocol_version == 0 or self.session is None:
            return None
        with self._lock:
            return self.session if self.session.ready else None

    def _check_ready(self):
        if self.ready and not self.ready_event.is_set():
            self.ready_event.set()
            session = self.session_or_none
            logger.info(
                "DAVE group established (epoch %s, members: %s)",
                session.epoch if session else "?",
                ", ".join(session.get_user_ids()) if session else "?",
            )

    def encrypt_frame(self, media_type, codec, frame: bytes) -> bytes:
        """Frames are sent transport-only until the DAVE group is
        established (and after a downgrade); receivers allow passthrough
        during transitions."""
        with self._lock:
            session = self.session_or_none
            return session.encrypt(media_type, codec, frame) if session else frame

    def _reinit_session(self):
        try:
            with self._lock:
                if self.protocol_version > 0:
                    if self.session is None:
                        self.session = davey.DaveSession(
                            self.protocol_version, self.user_id, self.channel_id
                        )
                    else:
                        self.session.reinit(
                            self.protocol_version, self.user_id, self.channel_id
                        )
                    if self._external_sender is not None:
                        self.session.set_external_sender(self._external_sender)
                    key_package = self.session.get_serialized_key_package()
                elif self.session is not None:
                    self.session.reset()
                    self.session.set_passthrough_mode(True, 10)
                    key_package = None
                else:
                    key_package = None
            if key_package:
                logger.info("sending MLS key package (%s bytes)", len(key_package))
                self.send_binary(MLS_KEY_PACKAGE, key_package)
        except Exception as e:
            logger.error("DAVE session reinit failed: %s", e)

    def _recover(self, transition_id: int):
        if self.reinitializing:
            return
        self.reinitializing = True
        logger.warning("Recovering from invalid commit/welcome (%s)", transition_id)
        self.send_json(DAVE_INVALID_COMMIT_WELCOME, {"transition_id": transition_id})
        self._reinit_session()

    # -- voice websocket events ------------------------------------------

    def on_session_description(self, dave_protocol_version: int):
        """op 4: the initial DAVE protocol version for this media session."""
        logger.info(
            "DAVE protocol version negotiated: %s (channel %s)",
            dave_protocol_version,
            self.channel_id,
        )
        self.protocol_version = dave_protocol_version
        self._reinit_session()

    def on_external_sender(self, payload: bytes):
        """op 25 binary: external sender credential + public key."""
        logger.info("DAVE external sender package (%s bytes)", len(payload))
        if self.session is None:
            self._external_sender = payload
            return
        try:
            with self._lock:
                self.session.set_external_sender(payload)
        except Exception as e:
            # AlreadyInGroup etc.; the gateway resends op 25 on group
            # recreation, where reinit resets the session anyway.
            logger.warning("Setting external sender failed: %s", e)

    def on_proposals(self, operation_type: int, payload: bytes):
        """op 27 binary: proposals to append or proposal refs to revoke."""
        if self.session is None:
            return
        optype = (
            davey.ProposalsOperationType.append
            if operation_type == 0
            else davey.ProposalsOperationType.revoke
        )
        logger.info("DAVE proposals: operation %s", operation_type)
        cw = None
        try:
            with self._lock:
                cw = self.session.process_proposals(
                    optype, payload, list(self.connected_user_ids)
                )
        except Exception as e:
            logger.warning("MLS proposals errored: %s", e)
            if self.last_transition_id is not None:
                self._recover(self.last_transition_id)
            return
        if cw is not None and cw.commit:
            logger.info(
                "DAVE committing proposals (commit %s bytes, welcome %s bytes)",
                len(cw.commit),
                len(cw.welcome or b""),
            )
            self.send_binary(MLS_COMMIT_WELCOME, cw.commit + (cw.welcome or b""))

    def on_commit(self, payload: bytes, transition_id: int):
        """op 29 binary: commit to process for an upcoming transition."""
        logger.info("DAVE commit for transition %s", transition_id)
        session = self.session
        if session is None:
            logger.warning(
                "Commit for transition %s before any session", transition_id
            )
            return
        try:
            with self._lock:
                session.process_commit(payload)
        except Exception as e:
            logger.warning("MLS commit errored (%s): %s", transition_id, e)
            return self._recover(transition_id)
        if transition_id != 0:
            self.pending_transitions[transition_id] = self.protocol_version
            self.send_json(DAVE_TRANSITION_READY, {"transition_id": transition_id})
        else:
            self.reinitializing = False
            self.last_transition_id = 0
        self._check_ready()

    def on_welcome(self, payload: bytes, transition_id: int):
        """op 30 binary: welcome adding us to the group."""
        logger.info("DAVE welcome for transition %s", transition_id)
        session = self.session
        if session is None:
            logger.warning(
                "Welcome for transition %s before any session", transition_id
            )
            return
        try:
            with self._lock:
                session.process_welcome(payload)
        except Exception as e:
            logger.warning("MLS welcome errored (%s): %s", transition_id, e)
            return self._recover(transition_id)
        if transition_id != 0:
            self.pending_transitions[transition_id] = self.protocol_version
            self.send_json(DAVE_TRANSITION_READY, {"transition_id": transition_id})
        else:
            self.reinitializing = False
            self.last_transition_id = 0
        self._check_ready()

    def on_prepare_transition(self, transition_id: int, protocol_version: int):
        """op 21 json: announces a (usually downgrade) transition."""
        self.pending_transitions[transition_id] = protocol_version
        if transition_id == 0:
            # (Re)initialization transition, executes immediately.
            return self._execute_transition(transition_id)
        if protocol_version == 0:
            # Downgrade upcoming: allow unencrypted frames on the receive
            # side for the duration of the transition.
            if self.session is not None:
                with self._lock:
                    self.session.set_passthrough_mode(True, 30)
        self.send_json(DAVE_TRANSITION_READY, {"transition_id": transition_id})

    def on_execute_transition(self, transition_id: int):
        """op 22 json: all participants are ready, execute the transition."""
        self._execute_transition(transition_id)

    def on_prepare_epoch(self, epoch: int, protocol_version: int):
        """op 24 json: epoch 1 means a new MLS group is being created."""
        if epoch == 1:
            self.protocol_version = protocol_version
            self._reinit_session()

    def on_clients_connect(self, user_ids):
        """op 11 json: user ids present in the media session."""
        self.connected_user_ids.update(int(uid) for uid in user_ids)

    def on_client_disconnect(self, user_id):
        """op 13 json."""
        self.connected_user_ids.discard(int(user_id))

    def _execute_transition(self, transition_id: int):
        if transition_id not in self.pending_transitions:
            logger.warning(
                "Execute transition %s without a pending transition", transition_id
            )
            return
        old_version = self.protocol_version
        self.protocol_version = self.pending_transitions.pop(transition_id)

        if old_version != self.protocol_version and self.protocol_version == 0:
            self.downgraded = True
        elif transition_id > 0 and self.downgraded:
            self.downgraded = False
            # Upgraded from transport-only: allow late unencrypted frames
            # for a short grace period.
            if self.session is not None:
                with self._lock:
                    self.session.set_passthrough_mode(True, 10)
