# Authorization values are credentials regardless of the authentication scheme.
s/(Authorization[[:space:]]*[:=][[:space:]]*).*/\1<redacted>/Ig

# Cookies are credentials as a whole, so do not attempt to preserve individual values.
s/((Set-Cookie|Cookie)[[:space:]]*:[[:space:]]*).*/\1<redacted>/Ig

# OAuth callback codes and state values are secrets only in URL query context.
s/([?&](code|state)=)[^&#[:space:]"']+/\1<redacted>/Ig

# Redact quoted JSON, YAML, and Python-dict credential values.
s/(["']?[[:alnum:]_.-]*(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|verification[_-]?token|encrypt[_-]?key|private[_-]?key|signing[_-]?key|access[_-]?key|api[_-]?key|password|passwd|ticket|token|secret|device[_-]?code|user[_-]?code|authorization[_-]?code|code[_-]?verifier|cookie|authorization)["']?[[:space:]]*[:=][[:space:]]*)["'][^"']*["']/\1<redacted>/Ig

# Redact key-value tuples emitted by Python representations of headers or queries.
s/(["']?[[:alnum:]_.-]*(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|verification[_-]?token|encrypt[_-]?key|private[_-]?key|signing[_-]?key|access[_-]?key|api[_-]?key|password|passwd|ticket|token|secret|device[_-]?code|user[_-]?code|authorization[_-]?code|code[_-]?verifier|cookie|authorization)["']?[[:space:]]*,[[:space:]]*)["'][^"']*["']/\1<redacted>/Ig

# Redact complete dotenv and YAML values, including values containing spaces.
s/^([[:space:]-]*["']?[[:alnum:]_.-]*(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|verification[_-]?token|encrypt[_-]?key|private[_-]?key|signing[_-]?key|access[_-]?key|api[_-]?key|password|passwd|ticket|token|secret|device[_-]?code|user[_-]?code|authorization[_-]?code|code[_-]?verifier|cookie|authorization)["']?[[:space:]]*[:=][[:space:]]*).*/\1<redacted>/Ig

# Redact unquoted values in logs and URL query strings.
s/([[:alnum:]_.-]*(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|verification[_-]?token|encrypt[_-]?key|private[_-]?key|signing[_-]?key|access[_-]?key|api[_-]?key|password|passwd|ticket|token|secret|device[_-]?code|user[_-]?code|authorization[_-]?code|code[_-]?verifier|cookie|authorization)[[:space:]]*[:=][[:space:]]*)[^,;}&[:space:]"']+/\1<redacted>/Ig
