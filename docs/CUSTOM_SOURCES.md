# Custom Sources

The idea is simple: custom `coq` sources are implemented via simple adapters.

The adapters turn various vim plugin's output into [LSP](https://microsoft.github.io/language-server-protocol/specification) `CompletionItem[] | CompletionList`.

## How to write a source:

```lua
-- `COQsources` is a global registry of sources
COQsources = COQsources or {}

COQsources["<random uid>"] = {
  name = "<name>"
  fn = function (callback)
    -- callback(<LSP completion items>) at some point
    local cancel = function ()
      ...
    end
    return cancel
  end
}
```