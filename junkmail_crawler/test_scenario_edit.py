"""Tests for Junk Mail scenario editor normalization."""
from __future__ import annotations

import unittest

from junkmail_crawler.scenario_edit import (
    normalize_junkmail_scenario_patch,
    normalize_junkmail_searches,
    normalize_search_keyword_fields,
    parse_comma_list,
    parse_or_groups,
)


class ScenarioEditTests(unittest.TestCase):
    def test_parse_comma_list(self) -> None:
        self.assertEqual(parse_comma_list("Laser, CO2, cutter"), ["laser", "co2", "cutter"])
        self.assertEqual(parse_comma_list(""), [])

    def test_parse_or_groups(self) -> None:
        self.assertEqual(
            parse_or_groups("iphone, samsung\nmacbook, dell"),
            [["iphone", "samsung"], ["macbook", "dell"]],
        )

    def test_rebuild_junkmail_url_from_fields(self) -> None:
        searches = normalize_junkmail_searches(
            [
                {
                    "name": "DTF printers",
                    "query": "dtf printer",
                    "category_path": "computers-and-gaming/gaming",
                    "category": "dtf-printing",
                    "min_price": 1000,
                    "max_price": 50000,
                    "private_only": True,
                }
            ]
        )
        self.assertEqual(len(searches), 1)
        row = searches[0]
        self.assertEqual(row["query"], "dtf printer")
        self.assertEqual(row["category_path"], "computers-and-gaming/gaming")
        self.assertTrue(row["private_only"])
        self.assertIn("/pr1000-50000/", row["url"])
        self.assertIn("q-dtf", row["url"])
        self.assertIn("computers-and-gaming/gaming", row["url"])

    def test_search_url_tracks_query_not_code_default(self) -> None:
        from junkmail_crawler.parsers import build_junkmail_search_url

        url = build_junkmail_search_url(
            min_price=0,
            max_price=999999,
            query="Python developer",
            category_path="jobs",
        )
        self.assertEqual(url, "https://www.junkmail.co.za/jobs/q-Python%20developer")
        self.assertNotIn("/q-python", url)
        searches = normalize_junkmail_searches(
            [
                {
                    "name": "Python developer",
                    "query": "Python developer",
                    "category_path": "jobs",
                    "category": "dev-jobs-python",
                    "min_price": 0,
                    "max_price": 999999,
                }
            ]
        )
        self.assertEqual(searches[0]["url"], url)

    def test_jobs_search_url_omits_price_segment(self) -> None:
        from junkmail_crawler.parsers import build_junkmail_search_url

        url = build_junkmail_search_url(
            min_price=0,
            max_price=999999,
            query="REACT",
            category_path="jobs",
        )
        self.assertEqual(url, "https://www.junkmail.co.za/jobs/q-REACT")
        searches = normalize_junkmail_searches(
            [
                {
                    "name": "React",
                    "query": "REACT",
                    "category_path": "jobs",
                    "category": "dev-jobs-react",
                    "min_price": 0,
                    "max_price": 999999,
                }
            ]
        )
        self.assertNotIn("/pr0-", searches[0]["url"])
        self.assertNotIn("job-seekers", searches[0]["url"])
        self.assertEqual(searches[0]["url"], "https://www.junkmail.co.za/jobs/q-REACT")

    def test_parser_skips_job_seeker_listings(self) -> None:
        from junkmail_crawler.parsers import is_junkmail_job_seeker_listing, parse_search_cards

        self.assertTrue(
            is_junkmail_job_seeker_listing(
                "/jobs/job-seekers/gauteng/pretoria/sales-executive/631cd53f264241e49c70ed5155f1b240"
            )
        )
        html = """
        <a href="/jobs/job-seekers/gauteng/pretoria/cv/631cd53f264241e49c70ed5155f1b240">CV</a>
        <a href="/jobs/information-technology/gauteng/pretoria/react-developer/abcdabcdabcdabcdabcdabcdabcdabcd">React dev</a>
        """
        cards = parse_search_cards(html, "https://www.junkmail.co.za/jobs/q-REACT", "dev-jobs-react", path_slugs=["jobs"])
        self.assertEqual(len(cards), 1)
        self.assertNotIn("job-seekers", cards[0]["url"])

    def test_parser_reads_jobmail_vacancy_cards(self) -> None:
        from junkmail_crawler.parsers import enrich_jobmail_listing_fields, parse_search_cards

        html = """
        <div class="card border-curve card-setup">
            <a class="card-image-link" href="https://www.jobmail.co.za/jobs/finance-accounting/finance-accounting-management/centurion/accountant-id-7496812?utm_source=Jobmail&amp;utm_medium=Referral&amp;utm_campaign=Jobmail_Referral" target="_blank">
                <img alt="Accountant" />
            </a>
            <a class="card-title font-weight-bold" href="https://www.jobmail.co.za/jobs/finance-accounting/finance-accounting-management/centurion/accountant-id-7496812?utm_source=Jobmail&amp;utm_medium=Referral&amp;utm_campaign=Jobmail_Referral" target="_blank">Accountant</a>
            <p class="badge badge-primary"><i class="fa fa-map-marker"></i>Centurion</p>
            <p class="badge badge-primary"><i class="fa fa-calendar"></i>136 minutes ago</p>
        </div>
        """
        cards = parse_search_cards(
            html,
            "https://www.junkmail.co.za/jobs/q-accountant",
            "dev-jobs-python",
            path_slugs=["jobs"],
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["ad_id"], "jobmail-7496812")
        self.assertEqual(
            cards[0]["url"],
            "https://www.jobmail.co.za/jobs/finance-accounting/finance-accounting-management/centurion/accountant-id-7496812",
        )
        self.assertEqual(cards[0]["title"], "Accountant")
        self.assertEqual(cards[0]["location"], "Centurion")

    def test_enrich_jobmail_listing_recovers_bad_detail_title(self) -> None:
        from junkmail_crawler.parsers import enrich_jobmail_listing_fields, merge_search_card_with_detail

        card = {
            "ad_id": "jobmail-7132766",
            "url": "https://www.jobmail.co.za/jobs/it-computer/development/western-cape/full-stack-developer-id-7132766",
            "title": "Full Stack Developer",
            "location": "Western Cape",
            "category": "dev-jobs-fullstack",
        }
        detail = {
            "title": "25489 Jobs in South Africa and Abroad on Job Mail",
            "location": None,
            "description": "",
        }
        merged = merge_search_card_with_detail(card, detail)
        self.assertEqual(merged["title"], "Full Stack Developer")
        self.assertEqual(merged["location"], "Western Cape")
        self.assertIn("Full Stack Developer", merged["description"])

        recovered = enrich_jobmail_listing_fields(
            {
                "ad_id": "jobmail-7132766",
                "url": card["url"],
                "title": "25489 Jobs in South Africa and Abroad on Job Mail",
                "location": None,
                "description": "",
            }
        )
        self.assertEqual(recovered["title"], "Full Stack Developer")
        self.assertEqual(recovered["location"], "Western Cape")
        self.assertTrue(recovered["description"])

    def test_reject_invalid_min_greater_than_max(self) -> None:
        with self.assertRaises(ValueError):
            normalize_junkmail_searches(
                [
                    {
                        "name": "Bad",
                        "category": "misc",
                        "min_price": 5000,
                        "max_price": 1000,
                    }
                ]
            )

    def test_normalize_scenario_patch_keywords(self) -> None:
        out = normalize_junkmail_scenario_patch(
            {
                "required_keywords_all": "Laser, cutter",
                "excluded_keywords": "broken, parts",
                "required_any_groups": "iphone, samsung\nmacbook",
            }
        )
        self.assertEqual(out["required_keywords_all"], ["laser", "cutter"])
        self.assertEqual(out["excluded_keywords"], ["broken", "parts"])
        self.assertEqual(out["required_any_groups"], [["iphone", "samsung"], ["macbook"]])

    def test_normalize_search_keyword_fields(self) -> None:
        out = normalize_search_keyword_fields(
            {
                "name": "Desktop computers",
                "required_keywords_all": "rtx, gpu",
                "excluded_keywords": "job vacancy, vacancy",
                "required_any_groups": "graphics card, gpu\nnvidia, amd",
            }
        )
        self.assertEqual(out["required_keywords_all"], ["rtx", "gpu"])
        self.assertEqual(out["excluded_keywords"], ["job vacancy", "vacancy"])
        self.assertEqual(out["required_any_groups"], [["graphics card", "gpu"], ["nvidia", "amd"]])

    def test_normalize_searches_with_per_search_keywords(self) -> None:
        searches = normalize_junkmail_searches(
            [
                {
                    "name": "Desktop computers",
                    "query": "gaming pc",
                    "category": "desktop-computers",
                    "min_price": 10000,
                    "max_price": 20000,
                    "excluded_keywords": "job vacancy, vacancy",
                    "required_any_groups": "rtx, gtx, gpu",
                }
            ]
        )
        self.assertEqual(searches[0]["excluded_keywords"], ["job vacancy", "vacancy"])
        self.assertEqual(searches[0]["required_any_groups"], [["rtx", "gtx", "gpu"]])


if __name__ == "__main__":
    unittest.main()
