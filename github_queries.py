"""GraphQL documents used by the GitHub contribution updater."""

USERNAME = "Lidang-Jiang"

ACCOUNT_QUERY = f"""
query {{
  user(login: "{USERNAME}") {{
    id
    databaseId
    createdAt
  }}
}}
"""

COMMIT_CONTRIBUTIONS_QUERY = f"""
query($from: DateTime!, $to: DateTime!) {{
  user(login: "{USERNAME}") {{
    contributionsCollection(from: $from, to: $to) {{
      totalCommitContributions
      totalRepositoriesWithContributedCommits
      commitContributionsByRepository(maxRepositories: 100) {{
        repository {{
          nameWithOwner
          stargazerCount
          url
          visibility
          owner {{ login }}
          defaultBranchRef {{ name }}
        }}
        contributions(first: 100) {{
          nodes {{ commitCount }}
          pageInfo {{ hasNextPage }}
        }}
      }}
    }}
  }}
}}
"""

MERGED_PRS_QUERY = f"""
query($cursor: String) {{
  user(login: "{USERNAME}") {{
    pullRequests(
      first: 100
      states: MERGED
      orderBy: {{field: CREATED_AT, direction: DESC}}
      after: $cursor
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number
        title
        url
        mergedAt
        baseRefName
        mergeCommit {{
          oid
          url
          messageHeadline
          message
          authoredDate
          author {{ email }}
          parents(first: 100) {{
            totalCount
            nodes {{ oid }}
          }}
          authors(first: 20) {{
            nodes {{ name email user {{ login }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        commits(first: 100) {{
          totalCount
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            commit {{
              oid
              url
              messageHeadline
              message
              authoredDate
              author {{ email }}
              authors(first: 20) {{
                nodes {{ name email user {{ login }} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
          }}
        }}
        repository {{
          nameWithOwner
          stargazerCount
          url
          visibility
          owner {{ login }}
          defaultBranchRef {{ name }}
        }}
      }}
    }}
  }}
}}
"""

BRANCH_HISTORY_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedRef: String!
  $authorId: ID!
  $cursor: String
) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $qualifiedRef) {
      target {
        __typename
        ... on Commit {
          history(first: 100, after: $cursor, author: {id: $authorId}) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              url
              messageHeadline
              authors(first: 100) {
                nodes { name email user { login } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
      }
    }
  }
}
"""

BRANCH_COAUTHOR_HISTORY_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedRef: String!
  $since: GitTimestamp!
  $cursor: String
) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $qualifiedRef) {
      target {
        __typename
        ... on Commit {
          history(
            first: 100
            after: $cursor
            since: $since
          ) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              url
              messageHeadline
              message
              authors(first: 100) {
                nodes { name email user { login } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
      }
    }
  }
}
"""

OBJECT_HISTORY_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!, $count: Int!) {
  repository(owner: $owner, name: $name) {
    object(oid: $oid) {
      __typename
      ... on Commit {
        history(first: $count) {
          pageInfo { hasNextPage endCursor }
          nodes {
            oid
            url
            messageHeadline
            message
            authoredDate
            author { email }
            authors(first: 100) {
              nodes { name email user { login } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }
    }
  }
}
"""

OPEN_PRS_QUERY = f"""
{{
  user(login: "{USERNAME}") {{
    pullRequests(first: 1, states: OPEN) {{
      totalCount
    }}
  }}
}}
"""
