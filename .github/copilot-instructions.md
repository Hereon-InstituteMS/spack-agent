# WSL Command Execution

This workspace lives in WSL, but terminal commands may be launched through
Windows PowerShell.

- For simple Linux commands, prefer direct invocation:
  `wsl.exe -d Ubuntu-24.04 -- <command> <args>`.
- Avoid `bash -lc` unless Bash syntax is necessary.
- Do not pass shell pipelines, semicolons, shell variables, or complex regular
  expression quoting through PowerShell when a direct command works.
- For multiline commands, pipes, redirects, or complex regular expressions,
  create or use a Bash script inside WSL rather than nesting PowerShell and
  Bash quotes.
- Treat PowerShell-to-WSL quoting failures as command-launch issues, not as
  evidence about the code or build.


# During code reviews, always verify the following:
- New functionality is covered by a (unit) test.
- Documentation is added (especially) for new code.
