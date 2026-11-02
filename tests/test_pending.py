"""Отложенные диктовки: очередь на диске и сторож повторной отправки."""

from __future__ import annotations

import json
import threading
import time

from whisperfree.pending import PendingStore, Resender


def store(tmp_path, **kwargs) -> PendingStore:
    return PendingStore(tmp_path / "pending", **kwargs)


class TestStore:
    def test_an_item_keeps_its_sound_and_description(self, tmp_path):
        s = store(tmp_path)
        item = s.add(b"FLAC-DATA", "speech.flac", "ru", 77.2, 1234.5)
        assert item is not None
        assert s.read(item) == b"FLAC-DATA"
        [loaded] = s.items()
        assert (loaded.lang, loaded.duration_s, loaded.record_ts) == ("ru", 77.2, 1234.5)

    def test_it_survives_a_restart(self, tmp_path):
        store(tmp_path).add(b"x", "a.flac", "ru", 1.0, 1.0)
        assert len(store(tmp_path).items()) == 1

    def test_oldest_first(self, tmp_path):
        s = store(tmp_path)
        first = s.add(b"1", "a.flac", "ru", 1.0, 1.0)
        time.sleep(0.01)
        s.add(b"2", "a.flac", "ru", 1.0, 2.0)
        assert s.items()[0].id == first.id

    def test_removed_item_leaves_nothing_behind(self, tmp_path):
        s = store(tmp_path)
        item = s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        s.remove(item)
        assert s.items() == []
        assert list((tmp_path / "pending").iterdir()) == []

    def test_a_failed_attempt_is_remembered(self, tmp_path):
        s = store(tmp_path)
        item = s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        s.touch(item, "сеть недоступна")
        [loaded] = store(tmp_path).items()
        assert (loaded.attempts, loaded.last_error) == (1, "сеть недоступна")

    def test_a_day_old_item_is_dropped(self, tmp_path):
        """Звук человека не должен копиться: очередь, а не архив."""
        s = store(tmp_path, max_age_s=60)
        item = s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        meta = tmp_path / "pending" / f"{item.id}.json"
        payload = json.loads(meta.read_text(encoding="utf-8"))
        payload["created"] -= 120
        meta.write_text(json.dumps(payload), encoding="utf-8")
        assert s.items() == []
        assert list((tmp_path / "pending").iterdir()) == []

    def test_the_queue_is_bounded(self, tmp_path):
        s = store(tmp_path, max_items=3)
        for n in range(5):
            s.add(b"x", "a.flac", "ru", 1.0, float(n))
            time.sleep(0.005)
        kept = s.items()
        assert len(kept) == 3
        assert [i.record_ts for i in kept] == [2.0, 3.0, 4.0]

    def test_a_description_without_sound_is_dropped(self, tmp_path):
        s = store(tmp_path)
        item = s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        (tmp_path / "pending" / f"{item.id}.flac").unlink()
        assert s.items() == []

    def test_a_broken_description_does_not_stop_the_rest(self, tmp_path):
        s = store(tmp_path)
        s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        (tmp_path / "pending" / "broken.json").write_text("{не json", encoding="utf-8")
        assert len(s.items()) == 1


class TestResender:
    """Когда пробовать снова. Саму отправку делает рабочий поток."""

    def make(self, tmp_path, outcomes):
        s = store(tmp_path)
        s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        attempts = []

        def submit(done: threading.Event):
            attempts.append(time.monotonic())
            if outcomes and outcomes.pop(0) == "ok":
                for item in s.items():
                    s.remove(item)
            done.set()

        r = Resender(s, submit, first_s=0.05, max_s=0.2)
        return s, r, attempts

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_it_tries_again_after_a_pause(self, tmp_path):
        s, r, attempts = self.make(tmp_path, ["ok"])
        r.start()
        try:
            assert self.wait_for(lambda: attempts), "повторной попытки не было"
            assert self.wait_for(lambda: len(s) == 0)
        finally:
            r.stop()

    def test_it_keeps_trying_while_the_network_is_down(self, tmp_path):
        s, r, attempts = self.make(tmp_path, ["fail", "fail", "ok"])
        r.start()
        try:
            assert self.wait_for(lambda: len(attempts) >= 3)
            assert self.wait_for(lambda: len(s) == 0)
        finally:
            r.stop()

    def test_the_pause_grows(self, tmp_path):
        s, r, attempts = self.make(tmp_path, ["fail"] * 4 + ["ok"])
        r.start()
        try:
            assert self.wait_for(lambda: len(attempts) >= 4)
        finally:
            r.stop()
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert gaps[-1] > gaps[0], f"пауза не растёт: {gaps}"

    def test_a_proven_network_means_now(self, tmp_path):
        s = store(tmp_path)
        s.add(b"x", "a.flac", "ru", 1.0, 1.0)
        attempts = []
        r = Resender(s, lambda done: attempts.append(1) or done.set(), first_s=30, max_s=60)
        r.start()
        try:
            r.online()
            assert self.wait_for(lambda: attempts, timeout=1.0), "online() не разбудил сразу"
        finally:
            r.stop()

    def test_a_fresh_failure_is_not_retried_at_once(self, tmp_path):
        """Сеть только что подвела — попытка через мгновение подведёт тоже."""
        s = store(tmp_path)
        attempts = []
        r = Resender(s, lambda done: attempts.append(1) or done.set(), first_s=30, max_s=60)
        r.start()
        try:
            s.add(b"x", "a.flac", "ru", 1.0, 1.0)
            r.added()
            time.sleep(0.3)
            assert attempts == []
        finally:
            r.stop()

    def test_nothing_waits_nothing_is_tried(self, tmp_path):
        s = store(tmp_path)
        attempts = []
        r = Resender(s, lambda done: attempts.append(1) or done.set(), first_s=0.02, max_s=0.05)
        r.start()
        try:
            r.online()
            time.sleep(0.2)
            assert attempts == []
        finally:
            r.stop()

    def test_stop_ends_the_thread(self, tmp_path):
        s, r, _ = self.make(tmp_path, [])
        r.start()
        r.stop()
        r._thread.join(timeout=2.0)
        assert not r._thread.is_alive()
