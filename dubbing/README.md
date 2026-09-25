# Shorts dubbing pipeline

Takes a finished English His Story / Her Story Short and produces a dubbed version that keeps
the original music and SFX, uses one cloned narrator voice in every language, translates with
fixed nicknames and names, and lands every line on the original timing.

```
src.mp4 ─► demucs ─► vocals.wav ─► Whisper large-v3 ─► sentence lines ─► Claude (glossary, syllable budget)
              │                                                              │
              └─► no_vocals.wav (music + SFX)                                ▼
                         │                                   TTS with the narrator clone, one clip per line
                         │                                                   │
                         │                     retime: trim, speed-up only on overrun (≤1.25x), level-match
                         ▼                                                   ▼
                      ffmpeg mix ◄───────────────────────────────── dub_vocals.<lang>.wav
                         │
                         ├─► dub.<lang>.mp4      (separate-channel upload)
                         └─► audio.<lang>.m4a    (YouTube multi-language audio track)
                                  │
                                  ▼
                qa: back-transcribe → WER, sped-up lines, overruns → qa.<lang>.json
```

## Usage

```bash
pip install -r requirements.txt           # plus ffmpeg on PATH
export ANTHROPIC_API_KEY=...  ELEVENLABS_API_KEY=...  ELEVENLABS_VOICE_ID=<narrator PVC id>

# full run: transcribe, translate, TTS, retime, mix, QA
python dub.py run short.mp4 --lang es --work work/18910 --glossary examples/18910_es/glossary.json

# with a pre-approved script and pre-rendered clips (how the test below was run)
python dub.py run short.mp4 --lang es --work work/18910 --script es_script.json --clips-dir clips/

# ElevenLabs Dubbing Studio end to end, for A/B
python dub.py eleven-dub short.mp4 --lang es --out eleven_es.m4a
```

Every stage caches its output in `--work`, so one Short's stems and transcript are computed
once and reused for all languages. A re-run only redoes stages whose output is missing.

## Test: `18910_99_his Final Render (2).mp4` → Spanish (2026-09-25)

98.9 s, 1080x1920, one narrator doing every voice.

| Stage | Result |
|---|---|
| Stem separation (htdemucs, 4 CPU cores) | 42 s. The vocal stem is clean speech with true silence between phrases. This Short's music/SFX bed is quiet (about −50 dBFS). |
| Whisper large-v3 | 135 s on CPU. **Hallucinated a repeated phrase at 88.5 s**, which a re-run of that window fixed. Now handled automatically: `condition_on_previous_text=False` plus a repeated-n-gram detector that re-transcribes the window. |
| Translation | 37 lines, neutral Latin American Spanish. Glossary: caca boy → *Niño Caca*, diarrhea boy → *Niño Diarrea*, bully → *bravucón*. The first draft ran up to 10.7 syllables/s on 9 lines, so the budget check forced rewrites down to ≤ 8.5. |
| Voice clone | Instant clone from 60 s of the vocal stem |
| TTS | Higgsfield `text2speech_v2`, MiniMax engine, one request per line. 7.95 credits ($0.72). |
| Retime | 16/37 lines sped up, median 1.04x, max 1.17x after one rewrite. 0 overruns. Max start delay 0.00 s. |
| QA | Back-transcription WER **0.3%**. Speaker similarity to the English narrator **0.91** (same-speaker baseline 0.99, different speakers usually 0.6–0.75). |

TTS engine shoot-out (same line, same clone). Similarity = Resemblyzer cosine to the narrator.
"es-ness" = Whisper language-ID probability, a proxy for how little English accent leaks into the Spanish.

| Engine | Similarity | es-ness | Duration (slot 8.44 s) | Intelligibility (WER) |
|---|---|---|---|---|
| ElevenLabs (via Higgsfield) | 0.82 | 0.999 | 8.56 s | 3% |
| **MiniMax** | **0.89** | **0.998** | **8.87 s** | **0%** |
| Seed Audio 1.0 | 0.96 | 0.987 | 9.76 s | 0% |
| Seed Speech | 0.97 | 0.955 | 10.68 s | 0% |
| Vibe Voice | 0.95 | 0.951 | 9.20 s | 0% |
| Cozy Voice | failed | | | |

The engines that sound most like the narrator also carry the most English accent. MiniMax was
the best balance. A Professional Voice Clone on ElevenLabs direct (trained on 30+ minutes of
clean narrator audio) should beat the instant clone on similarity; that is the next A/B.

Higgsfield `dubbing` (translate + voice + lip-sync, one call): the full 99 s Short **failed**
after about 16 minutes and was refunded. The cost is 353 credits (about $32, $19.50/min).
See the summary in the PR or session for the 15 s retry.

ElevenLabs Dubbing Studio: not run yet, because the environment has no `ELEVENLABS_API_KEY`.
`python dub.py eleven-dub` is ready for it.

## Files

- `dub.py`: the pipeline (stages above, plus `eleven-dub`)
- `examples/18910_es/`: raw English transcript, approved Spanish script, glossary, timing report, QA report
