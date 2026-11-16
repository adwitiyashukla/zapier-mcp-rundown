# morning rundown

One ask that pulls from GitHub and Gmail and hands back one combined rundown: profile and repo health from GitHub, newest inbox mail with unread count and top senders from Gmail.

## Validated fixed values

- GitHub: selected_api GitHubCLIAPI, default connection set on the server (account adwitiyashukla)
- Gmail: selected_api GoogleMailV2CLIAPI, default connection set on the server (the personal Gmail account connected there)
- GitHub owner: adwitiyashukla (the connected account, so the owner param can stay empty)
- Repos checked in detail: marketpay, product-search-engine, lsm-storage-engine
- Gmail query: in:inbox (returns up to 100 newest inbox messages in one call, each with subject, from, date and labels)
- tool_name values: github_find_user, github_find_repository, github_find_issue (used only to resolve the repo dropdown), gmail_find_email

## Actions

1. ZapierAction[GitHub:user]()
   Returns the profile. Use name, public_repos, followers, following.

2. Repo list. This is not an action. Call inspect_zapier_actions with tool_name "github_find_issue" and enum_property "repo". The dynamic_enum_values are every repo as owner/name. Follow dynamic_enum_next_cursor if it is present so the count is complete.

3. ZapierAction[GitHub:repository_v2](repo: "marketpay")
   ZapierAction[GitHub:repository_v2](repo: "product-search-engine")
   ZapierAction[GitHub:repository_v2](repo: "lsm-storage-engine")
   Use language, pushed_at, open_issues_count, stargazers_count, license.

4. ZapierAction[Gmail:message](query: "in:inbox")
   Returns a results list. Use subject, from.name, from.email, date, labels. Ignore raw, body_html and body_plain; they are large and not needed.

## Runtime instructions

1. Run actions 1, 3 and 4 together; they share no inputs. Run step 2 alongside them.
2. From action 4: count results whose labels include UNREAD, take the latest six (date, sender name, subject), and count senders by from.email to name the top five.
3. Print one block headed "Rundown for <today's date>". First a GitHub paragraph: repo count, then for each of the three repos the language, last push date, open issues and license. Then a Gmail line with how many mails came back, the date range they cover and the unread count, followed by a table of the latest six and one line of top senders.
4. Close with one line on anything worth acting on, for example a repo with no license or a job alert buried under newsletters.
5. Show only sources that returned data. If Gmail returns nothing for in:inbox, say so in one line instead of narrowing the query.

## Constraints

- Do not re-resolve the connections or ask which account to use. The defaults set on the server are correct.
- Do not use New Email or any other trigger style read. Find Email with in:inbox is the read.
- Do not add filters such as is:unread or newer_than to the Gmail query unless the user asks for them.
- Do not print email bodies. Subject, sender and date only.
- Plain ASCII in the output. No em dashes, no emojis, no bullets, no arrows.
- If the user asks to change the three repos, swap the names in action 3 and update this skill so the saved version matches.
