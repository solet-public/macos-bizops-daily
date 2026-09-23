Generate a descriptive, human-friendly filename for the requested artifact.

Rules:
- Name should reflect the content or purpose
- Use lowercase with underscores for spaces
- Keep names concise (1-4 words)
- Avoid generic names like 'output' or 'file'
- If input_filename is provided and acceptable, use it as-is

Respond with a JSON object containing:
- display_name: The proposed base name (without extension)
- extension: File extension based on artifact_type (e.g., 'wav', 'png')
- confidence: 'high', 'medium', or 'low'
- flags: Array of any concerns (e.g., 'generic', 'ambiguous')
