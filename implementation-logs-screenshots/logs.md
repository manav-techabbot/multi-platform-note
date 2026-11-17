# Logs for the completion of this task

## Went through the instruction

### What I understood

Create an api/app which takes a note and then publishes that note to multiple platforms such as linkedin, facebook, Instagram, reddit, telegram, Blogger, etc.

### What needs to be done

- Research what platform has documented and dedicated API to post on their platforms (linkedin, twitter, etc)
- Get their API Keys to use it.
- Create our api endpoint which takes the note
- Handle all the exceptions and retried, if one or more apis fail etc.
- Handle the rate limiting of their apis through our api endpoint.

---

## Research & Implementation

Started doing RnD for the platform which allow free and documented APIs - Used google and chatgpt for the research.

Found the Jsonplaceholder platform which allows crud requests but only simulated.

Then founnd the bluesky social media and tumblr for uploading posts

Looked into Linkedin, instagram, Facebook, twitter, but none provide documented free tier APIs for posting.

Finally settled on Tumblr, BlueSky, Github Gist, Dev community and Started Researching the Api docs.

Tumblr docs was the most challenging as there was an OAuth flow instead of a straight foward api key so Implemented by going through the docs and implementing Oauth flow and endpoints.

Looked into typed validaiton for pydantic and used that

added retries and maximum retries as it would be working in production with correct retries and global exception handling.

Finally decided to build and integerate all the publishers(platform) together

decided to use a modular approach where each publisher has its own code where it has its own business logic/ flow etc And then gets built in registry.

all the platforms are checked from env in enabled platforms and then for each platform the client uses its logic to post the request.


## Exception handling and retry

Making sure that the Apis run parallely so one api doesnt stop the others.

Also making sure to properly catch errors for each platform so one api fail does not stop other, and also is retrying if the response is 500 or similar (server error) it should NOT retry if the response is 400 or similar because thats a user error.

Have to also make sure that the ratelimit of the apis are not affected so if the 429 is there as response it will retry again after a configured amount of time from env (RETRY_AFTER) key.

If the error is coming from network like response error, it will retry, but if it is a local server error it will handle that exception, log it and not retry.

## Logging 

need to have different levels and types of logs, like for any running line, like program started, request started etc, there is INFO
For retry and failed status there is WARNING.
And for exceptions from server there is ERROR

Trying to implement a global logging services which is used by every other module and passing just log msg and type.