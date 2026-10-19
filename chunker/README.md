# BC Gov SSO Documentation Preprocessor

A heading-aware Markdown chunking tool designed for the **BC Gov SSO documentation site** (`bcgov/sso-docs`).

This utility converts Docusaurus/Markdown documentation into Amazon Bedrock Knowledge Base compatible chunks by:

- Preserving document hierarchy and context
- Generating one Markdown file per chunk
- Creating Bedrock metadata sidecar files (`.metadata.json`)
- Rewriting internal documentation links to absolute URLs
- Cleaning Docusaurus/MDX-specific syntax
- Producing chunk sizes optimized for embedding and retrieval

The generated output is intended for upload to S3 and ingestion into an Amazon Bedrock Knowledge Base configured with **Chunking Strategy = NONE**.

---

## Features

### Hierarchical Chunking

The chunker understands documentation structure and uses headings as semantic boundaries.

### Breadcrumb Context

Every chunk receives a contextual breadcrumb:

```text
Authentication > IDIR > Login Flow
```

This improves embedding quality and retrieval accuracy by preserving document hierarchy inside the chunk itself.

### Smart Splitting

The processor avoids splitting:

- Fenced code blocks
- Markdown tables
- Heading structures

When a section exceeds the configured token budget it is split intelligently using subsection, paragraph, list, code, table-row, and sentence boundaries.

### Smart Merging

Small neighbouring sections are merged to prevent creation of low-value chunks.

### FAQ Awareness

FAQ pages receive special handling with one question per chunk.

### Bedrock Metadata Generation

Each generated chunk has a matching `.metadata.json` sidecar file.

---

## Requirements

### Python

Python 3.10+

### Dependencies

Required:

```bash
pip install pyyaml
```

Optional:

```bash
pip install tiktoken
```

---

## Usage

Generate chunks:

```bash
python3 preprocess.py --repo ./sso-docs --out ./chunks
```

Exclude folders:

```bash
python3 preprocess.py --repo ./sso-docs --out ./chunks --exclude 'releases/*'
```

Upload results to S3:

```bash
aws s3 sync ./chunks s3://<bucket>/sso-docs/ --delete
```

---

## Output

The script generates:

```text
chunks/
├── chunk.md
├── chunk.md.metadata.json
└── ...
```

It also produces:

```text
chunks_manifest.jsonl
```

containing metadata for all generated chunks.

---

## Recommended Bedrock Configuration

Use:

```text
Knowledge Base
└── Data Source
     ├── S3 Bucket
     ├── Chunking Strategy = NONE
     └── Metadata Files = Enabled
```

The script already performs semantic chunking and metadata enrichment, so additional Bedrock chunking is not recommended.

---

## Key Capabilities

- Preserves heading hierarchy
- Preserves code blocks and tables
- Rewrites relative links
- Adds breadcrumb context
- Generates Bedrock metadata sidecars
- Supports FAQ-specific chunking
- Extracts release metadata
- Generates category overview chunks
- Produces ingestion-ready content for Amazon Bedrock Knowledge Bases
