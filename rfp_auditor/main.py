"""
main.py
────────
Developer A standalone CLI entry point.

Use this to run the full pipeline without Gradio (useful for testing
the AI + PDF ingestion chain independently of Dev B's UI).

Usage:
    python main.py --pdf path/to/rfp.pdf --start 1 --end 20

In production, Dev B's Gradio UI replaces this CLI by calling
core_ai.backend_bridge.run_audit_pipeline() directly.
"""

import argparse
import json
import sys
from pathlib import Path

from config.settings import get_logger, validate_env
from core_ai.backend_bridge import run_audit_pipeline
from shared.schemas import AuditJobConfig

logger = get_logger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RFP Auditor — Developer A CLI (Phase 1-4 pipeline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --health-check
  python main.py --pdf ./sample_rfp.pdf --start 1 --end 15
  python main.py --pdf ./sample_rfp.pdf --output ./output/results.json
        """,
    )
    parser.add_argument("--pdf", type=str, help="Path to the RFP PDF file.")
    parser.add_argument("--start", type=int, default=1, help="Start page (1-based). Default: 1")
    parser.add_argument("--end", type=int, default=None, help="End page (1-based). Default: last page")
    parser.add_argument("--output", type=str, default="output/audit_results.json",
                        help="Output JSON path for audit results. Default: output/audit_results.json")
    parser.add_argument("--health-check", action="store_true",
                        help="Run Gemini API health check only (no PDF needed).")
    return parser.parse_args()


def run_health_check() -> None:
    """Phase 1: Test Gemini API connectivity and latency."""
    from core_ai.gemini_client import GeminiClient
    logger.info("=== Gemini API Health Check ===")
    client = GeminiClient()
    result = client.health_check()
    for k, v in result.items():
        logger.info(f"  {k}: {v}")
    if result["status"] != "OK":
        sys.exit(1)


def main() -> None:
    args = parse_args()

    if args.health_check:
        run_health_check()
        return

    if not args.pdf:
        print("Error: --pdf is required unless running --health-check")
        sys.exit(1)

    # Validate environment early
    try:
        validate_env()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    config = AuditJobConfig(
        pdf_path=args.pdf,
        start_page=args.start,
        end_page=args.end,
        output_path=args.output,
    )

    # Create output directory
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    logger.info("=== RFP Auditor — Developer A Pipeline ===")
    logger.info(f"PDF: {config.pdf_path}")
    logger.info(f"Pages: {config.start_page} → {config.end_page or 'end'}")

    final_results = None
    for status_msg, results in run_audit_pipeline(config, web_context_fn=None):
        logger.info(status_msg)
        if results is not None:
            final_results = results

    if final_results:
        # Serialize to JSON (Dev B's excel_exporter will later read structured data)
        output_data = [row.model_dump() for row in final_results]
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Results saved to: {args.output}")
        logger.info(f"Total rows: {len(output_data)}")
    else:
        logger.warning("Pipeline produced no results.")


if __name__ == "__main__":
    main()
