Output docs/assets/agents-demo.gif

Require uv

Set Shell "bash"
Set FontSize 16
Set LineHeight 1.15
Set Width 920
Set Height 830
Set Padding 18
Set Framerate 12
Set TypingSpeed 35ms
Set Theme "GitHub Dark"

Hide
Type "uv run python -c pass && clear"
Enter
Wait@120s
Show

Type "uv run python examples/agents-demo/demo.py"
Sleep 400ms
Enter
Wait@90s
Sleep 6s
