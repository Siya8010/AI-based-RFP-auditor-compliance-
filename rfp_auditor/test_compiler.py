from core_ai.query_compiler import QueryCompiler
from core_ai.gemini_client import GeminiClient
from shared.schemas import ParsedSegment

# Initialize clients
client = GeminiClient()
compiler = QueryCompiler(client)

# Define the test segment
seg = ParsedSegment(
    page_number=1, 
    block_index=0,
    raw_text="The system shall support 10GbE uplink ports with LACP bonding per IEEE 802.3ad",
    bbox=(0,0,100,20)
)

# Execute and display the output
print(compiler.compile_queries([seg]))
