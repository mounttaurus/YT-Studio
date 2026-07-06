"""
ユニットテスト: YouTube SEOオプティマイザの純関数。

stdlib unittest を使用（pytest依存なし）。
対象: app.core.seo_optimizer の集計関数＆app.core.youtube_client の台帳管理。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo


# テスト対象モジュールをimport（環境変数依存でエラーになった場合は報告）
try:
    from app.core import seo_optimizer, youtube_client
except ImportError as e:
    print(f"[ImportError] {e}")
    raise


class TestAggregateTags(unittest.TestCase):
    """seo_optimizer.aggregate_tags の正規化・集計・上位40件打ち切り"""

    def test_basic_aggregation(self):
        """タグの正規化と頻度集計"""
        video_items = [
            {
                "snippet": {
                    "tags": ["AI", "Tech", "Machine Learning"]
                }
            },
            {
                "snippet": {
                    "tags": ["ai", "TECH", "python"]
                }
            },
        ]
        result = seo_optimizer.aggregate_tags(video_items)

        # 結果は {"tag": str, "count": int} の配列
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(r, dict) for r in result))
        self.assertTrue(all("tag" in r and "count" in r for r in result))

        # 英字は小文字化
        tag_names = [r["tag"] for r in result]
        self.assertIn("ai", tag_names)
        self.assertIn("tech", tag_names)
        self.assertIn("machine learning", tag_names)

        # 同じタグは集計（ai, AI → "ai": 2）
        counts = {r["tag"]: r["count"] for r in result}
        self.assertEqual(counts["ai"], 2)
        self.assertEqual(counts["tech"], 2)

    def test_japanese_tags_preserved(self):
        """日本語タグはそのまま（小文字化しない）"""
        video_items = [
            {"snippet": {"tags": ["都市伝説", "心霊"]}},
            {"snippet": {"tags": ["都市伝説", "怖い話"]}},
        ]
        result = seo_optimizer.aggregate_tags(video_items)
        tag_names = [r["tag"] for r in result]

        self.assertIn("都市伝説", tag_names)
        self.assertIn("心霊", tag_names)
        self.assertIn("怖い話", tag_names)

    def test_whitespace_stripped(self):
        """前後の空白は除去"""
        video_items = [
            {"snippet": {"tags": ["  AI  ", " Tech "]}},
        ]
        result = seo_optimizer.aggregate_tags(video_items)
        tag_names = [r["tag"] for r in result]

        self.assertIn("ai", tag_names)
        self.assertIn("tech", tag_names)
        self.assertNotIn("  ai  ", tag_names)

    def test_empty_tags_skipped(self):
        """空文字タグはスキップ"""
        video_items = [
            {"snippet": {"tags": ["AI", "", "Tech"]}},
        ]
        result = seo_optimizer.aggregate_tags(video_items)
        tag_names = [r["tag"] for r in result]

        self.assertNotIn("", tag_names)

    def test_truncate_to_40(self):
        """上位40件で打ち切り"""
        tags = [f"tag_{i:02d}" for i in range(100)]
        video_items = [{"snippet": {"tags": tags}}]
        result = seo_optimizer.aggregate_tags(video_items)

        self.assertEqual(len(result), 40)

    def test_missing_snippet_safe(self):
        """snippet が無いアイテムは安全にスキップ"""
        video_items = [
            {"snippet": {"tags": ["AI"]}},
            {},  # snippet なし
            {"snippet": None},  # snippet は None
        ]
        result = seo_optimizer.aggregate_tags(video_items)

        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)  # AI は含まれる


class TestRankChannels(unittest.TestCase):
    """seo_optimizer.rank_channels の競合判定・上位15件"""

    def test_basic_ranking(self):
        """チャンネル ID の出現回数で順位付け"""
        video_items = [
            {"snippet": {"channelId": "ch_001"}},
            {"snippet": {"channelId": "ch_001"}},
            {"snippet": {"channelId": "ch_002"}},
            {"snippet": {"channelId": "ch_003"}},
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "AI Channel"},
                "statistics": {"subscriberCount": "1000000"}
            },
            {
                "id": "ch_002",
                "snippet": {"title": "Tech Channel"},
                "statistics": {"subscriberCount": "500000"}
            },
            {
                "id": "ch_003",
                "snippet": {"title": "Learning Channel"},
                "statistics": {"subscriberCount": "200000"}
            },
        ]
        result = seo_optimizer.rank_channels(video_items, channel_items)

        # 出現回数順（ch_001 > ch_002 > ch_003）
        self.assertEqual(result[0]["channel_id"], "ch_001")
        self.assertEqual(result[0]["appearances"], 2)
        self.assertEqual(result[1]["channel_id"], "ch_002")
        self.assertEqual(result[1]["appearances"], 1)

    def test_hidden_subscriber_count(self):
        """hiddenSubscriberCount フラグ時は None に"""
        video_items = [
            {"snippet": {"channelId": "ch_001"}},
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "Secret Channel"},
                "statistics": {
                    "subscriberCount": "??",
                    "hiddenSubscriberCount": True
                }
            },
        ]
        result = seo_optimizer.rank_channels(video_items, channel_items)

        self.assertEqual(result[0]["subscribers"], None)

    def test_truncate_to_15(self):
        """上位15件で打ち切り"""
        video_items = [
            {"snippet": {"channelId": f"ch_{i:03d}"}}
            for i in range(50)
        ]
        channel_items = [
            {
                "id": f"ch_{i:03d}",
                "snippet": {"title": f"Channel {i}"},
                "statistics": {"subscriberCount": "1000"}
            }
            for i in range(50)
        ]
        result = seo_optimizer.rank_channels(video_items, channel_items)

        self.assertEqual(len(result), 15)

    def test_missing_channelid_skipped(self):
        """channelId が無い動画はスキップ"""
        video_items = [
            {"snippet": {"channelId": "ch_001"}},
            {"snippet": {}},  # channelId なし
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "Channel"},
                "statistics": {"subscriberCount": "1000"}
            },
        ]
        result = seo_optimizer.rank_channels(video_items, channel_items)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["channel_id"], "ch_001")


class TestFindUpsets(unittest.TestCase):
    """seo_optimizer.find_upsets の下克上動画検出（比率≥2.0）"""

    def test_ratio_calculation(self):
        """views ÷ max(subscribers, 1) の計算"""
        video_items = [
            {
                "id": "vid_001",
                "snippet": {
                    "title": "Small channel big hit",
                    "channelId": "ch_001",
                    "channelTitle": "Small Channel"
                },
                "statistics": {"viewCount": "2000000"}  # 2M views
            },
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "Small Channel"},
                "statistics": {"subscriberCount": "100000"}  # 100K subs
            },
        ]
        result = seo_optimizer.find_upsets(video_items, channel_items)

        # ratio = 2000000 / 100000 = 20.0
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ratio"], 20.0)

    def test_threshold_2_0(self):
        """ratio < 2.0 は除外"""
        video_items = [
            {
                "id": "vid_001",
                "snippet": {
                    "title": "Normal video",
                    "channelId": "ch_001",
                    "channelTitle": "Channel"
                },
                "statistics": {"viewCount": "100000"}  # 100K views
            },
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "Channel"},
                "statistics": {"subscriberCount": "100000"}  # 100K subs
            },
        ]
        result = seo_optimizer.find_upsets(video_items, channel_items)

        # ratio = 100000 / 100000 = 1.0 < 2.0 → 除外
        self.assertEqual(len(result), 0)

    def test_zero_subscribers_safe(self):
        """購読者0でも安全（1で割る）"""
        video_items = [
            {
                "id": "vid_001",
                "snippet": {
                    "title": "New channel hit",
                    "channelId": "ch_001",
                    "channelTitle": "New Channel"
                },
                "statistics": {"viewCount": "5000000"}
            },
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "New Channel"},
                "statistics": {"subscriberCount": "0"}
            },
        ]
        result = seo_optimizer.find_upsets(video_items, channel_items)

        # ratio = 5000000 / max(0, 1) = 5000000 >= 2.0
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ratio"], 5000000.0)

    def test_truncate_to_10(self):
        """上位10件で打ち切り、比率降順"""
        video_items = [
            {
                "id": f"vid_{i:03d}",
                "snippet": {
                    "title": f"Video {i}",
                    "channelId": f"ch_{i:03d}",
                    "channelTitle": f"Channel {i}"
                },
                "statistics": {"viewCount": str((i + 10) * 1000000)}
            }
            for i in range(20)
        ]
        channel_items = [
            {
                "id": f"ch_{i:03d}",
                "snippet": {"title": f"Channel {i}"},
                "statistics": {"subscriberCount": "1000000"}
            }
            for i in range(20)
        ]
        result = seo_optimizer.find_upsets(video_items, channel_items)

        self.assertEqual(len(result), 10)
        # 比率が降順
        self.assertTrue(all(
            result[i]["ratio"] >= result[i + 1]["ratio"]
            for i in range(len(result) - 1)
        ))

    def test_none_subscribers_treated_as_zero(self):
        """subscriberCount が None なら 0 扱い"""
        video_items = [
            {
                "id": "vid_001",
                "snippet": {
                    "title": "Video",
                    "channelId": "ch_001",
                    "channelTitle": "Channel"
                },
                "statistics": {"viewCount": "5000000"}
            },
        ]
        channel_items = [
            {
                "id": "ch_001",
                "snippet": {"title": "Channel"},
                "statistics": {"subscriberCount": None}
            },
        ]
        result = seo_optimizer.find_upsets(video_items, channel_items)

        self.assertEqual(len(result), 1)


class TestYoutubeClientLedger(unittest.TestCase):
    """youtube_client の台帳管理（クォータ・日次リセット）"""

    def setUp(self):
        """テスト用一時ディレクトリを作成"""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmpdir.name) / "youtube_cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.ledger_path = self.cache_dir / "quota_ledger.json"

    def tearDown(self):
        """一時ディレクトリを削除"""
        self.tmpdir.cleanup()

    def test_ledger_date_reset(self):
        """日付が変わるとused=0にリセット"""
        with patch.object(youtube_client, "LEDGER_PATH", self.ledger_path):
            with patch.object(youtube_client, "CACHE_DIR", self.cache_dir):
                # 初期: 今日の日付で記録
                today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                ledger = {"date_pt": today_str, "used": 5000}
                self.ledger_path.write_text(json.dumps(ledger))

                # read
                loaded = youtube_client._load_ledger()
                self.assertEqual(loaded["date_pt"], today_str)
                self.assertEqual(loaded["used"], 5000)

                # 日付が変わった場合（_today_pt をパッチ）
                yesterday_str = (
                    datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                ledger["date_pt"] = yesterday_str
                self.ledger_path.write_text(json.dumps(ledger))

                with patch.object(youtube_client, "_today_pt", return_value=today_str):
                    reloaded = youtube_client._load_ledger()
                    self.assertEqual(reloaded["date_pt"], today_str)
                    self.assertEqual(reloaded["used"], 0)  # リセット

    def test_quota_budget_exceeded(self):
        """予算超過時に例外を送出"""
        with patch.object(youtube_client, "LEDGER_PATH", self.ledger_path):
            with patch.object(youtube_client, "CACHE_DIR", self.cache_dir):
                with patch.object(youtube_client, "DAILY_QUOTA_BUDGET", 1000):
                    today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                    ledger = {"date_pt": today_str, "used": 950}
                    self.ledger_path.write_text(json.dumps(ledger))

                    # 100 消費しようとする（950 + 100 > 1000）
                    with self.assertRaises(youtube_client.QuotaBudgetExceeded):
                        youtube_client._check_and_reserve(100)

    def test_quota_reserve_success(self):
        """予算内なら記帳に成功"""
        with patch.object(youtube_client, "LEDGER_PATH", self.ledger_path):
            with patch.object(youtube_client, "CACHE_DIR", self.cache_dir):
                with patch.object(youtube_client, "DAILY_QUOTA_BUDGET", 1000):
                    today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                    ledger = {"date_pt": today_str, "used": 900}
                    self.ledger_path.write_text(json.dumps(ledger))

                    # 50 消費（900 + 50 <= 1000）
                    youtube_client._check_and_reserve(50)

                    # 台帳が更新されている
                    updated = json.loads(self.ledger_path.read_text())
                    self.assertEqual(updated["used"], 950)

    def test_bom_handling(self):
        """BOM付きUTF-8でも台帳カウントが維持される"""
        with patch.object(youtube_client, "LEDGER_PATH", self.ledger_path):
            with patch.object(youtube_client, "CACHE_DIR", self.cache_dir):
                # BOM付きUTF-8で書き込み
                today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                ledger = {"date_pt": today_str, "used": 1234}
                bom = b'\xef\xbb\xbf'
                content = json.dumps(ledger).encode("utf-8")
                self.ledger_path.write_bytes(bom + content)

                # 読み込み（utf-8-sig で BOM を自動削除）
                loaded = youtube_client._load_ledger()
                self.assertEqual(loaded["used"], 1234)

    def test_quota_status(self):
        """quota_status() が正しい残高を返す"""
        with patch.object(youtube_client, "LEDGER_PATH", self.ledger_path):
            with patch.object(youtube_client, "CACHE_DIR", self.cache_dir):
                with patch.object(youtube_client, "DAILY_QUOTA_BUDGET", 8000):
                    today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                    ledger = {"date_pt": today_str, "used": 3000}
                    self.ledger_path.write_text(json.dumps(ledger))

                    status = youtube_client.quota_status()
                    self.assertEqual(status["used"], 3000)
                    self.assertEqual(status["budget"], 8000)
                    self.assertEqual(status["remaining"], 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
