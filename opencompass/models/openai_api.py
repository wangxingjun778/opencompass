import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Dict, List, Optional, Union

import httpx
import jieba
import requests

from opencompass.registry import MODELS
from opencompass.utils.prompt import PromptList

from .base_api import BaseAPIModel
from opencompass.utils.clients import OpenAIClientUtil, XRequestConfig

PromptType = Union[PromptList, str]
OPENAI_API_BASE = os.path.join(
    os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1/'),
    'chat/completions')

O1_MODEL_LIST = [
    'o1-preview-2024-09-12',
    'o1-mini-2024-09-12',
    'o1-preview',
    'o1-mini',
]


@MODELS.register_module()
class OpenAI(BaseAPIModel):
    """Model wrapper around OpenAI's models.

    Args:
        path (str): The name of OpenAI's model.
        max_seq_len (int): The maximum allowed sequence length of a model.
            Note that the length of prompt + generated tokens shall not exceed
            this value. Defaults to 2048.
        query_per_second (int): The maximum queries allowed per second
            between two consecutive calls of the API. Defaults to 1.
        retry (int): Number of retires if the API call fails. Defaults to 2.
        key (str or List[str]): OpenAI key(s). In particular, when it
            is set to "ENV", the key will be fetched from the environment
            variable $OPENAI_API_KEY, as how openai defaults to be. If it's a
            list, the keys will be used in round-robin manner. Defaults to
            'ENV'.
        org (str or List[str], optional): OpenAI organization(s). If not
            specified, OpenAI uses the default organization bound to each API
            key. If specified, the orgs will be posted with each request in
            round-robin manner. Defaults to None.
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
        openai_api_base (str): The base url of OpenAI's API. Defaults to
            'https://api.openai.com/v1/chat/completions'.
        openai_proxy_url (str, optional): An optional proxy url to use when
            connecting to OpenAI's API. When set to 'ENV', the url will be
            fetched from the environment variable $OPENAI_PROXY_URL.
            Defaults to None.
        mode (str, optional): The method of input truncation when input length
            exceeds max_seq_len. 'front','mid' and 'rear' represents the part
            of input to truncate. Defaults to 'none'.
        temperature (float, optional): What sampling temperature to use.
            If not None, will override the temperature in the `generate()`
            call. Defaults to None.
        tokenizer_path (str, optional): The path to the tokenizer. Use path if
            'tokenizer_path' is None, otherwise use the 'tokenizer_path'.
            Defaults to None.
        extra_body (Dict, optional): Add additional JSON properties to
            the request
    """

    is_api: bool = True

    def __init__(self,
                 path: str = 'gpt-3.5-turbo',
                 max_seq_len: int = 4096,
                 query_per_second: int = 1,
                 rpm_verbose: bool = False,
                 retry: int = 2,
                 key: Union[str, List[str]] = 'ENV',
                 org: Optional[Union[str, List[str]]] = None,
                 meta_template: Optional[Dict] = None,
                 openai_api_base: str = OPENAI_API_BASE,
                 openai_proxy_url: Optional[str] = None,
                 mode: str = 'none',
                 logprobs: Optional[bool] = False,
                 top_logprobs: Optional[int] = None,
                 temperature: Optional[float] = 0.0,
                 tokenizer_path: Optional[str] = None,
                 extra_body: Optional[Dict] = None,
                 max_completion_tokens: int = 16384,
                 verbose: bool = False,
                 is_chat: bool = True,
                 **kwargs):

        super().__init__(path=path,
                         max_seq_len=max_seq_len,
                         meta_template=meta_template,
                         query_per_second=query_per_second,
                         rpm_verbose=rpm_verbose,
                         retry=retry,
                         verbose=verbose)
        import tiktoken
        self.tiktoken = tiktoken
        self.temperature = temperature
        assert mode in ['none', 'front', 'mid', 'rear']
        self.mode = mode
        self.logprobs = logprobs
        self.top_logprobs = top_logprobs
        self.tokenizer_path = tokenizer_path
        self.hf_tokenizer = None
        self.extra_body = extra_body

        if isinstance(key, str):
            if key == 'ENV':
                if 'OPENAI_API_KEY' not in os.environ:
                    raise ValueError('OpenAI API key is not set.')
                self.keys = os.getenv('OPENAI_API_KEY').split(',')
            else:
                self.keys = [key]
        else:
            self.keys = key

        # record invalid keys and skip them when requesting API
        # - keys have insufficient_quota
        self.invalid_keys = set()

        self.key_ctr = 0
        if isinstance(org, str):
            self.orgs = [org]
        else:
            self.orgs = org
        self.org_ctr = 0
        self.url = openai_api_base

        if openai_proxy_url == 'ENV':
            if 'OPENAI_PROXY_URL' not in os.environ:
                raise ValueError('OPENAI_PROXY_URL is not set.')
            self.proxy_url = os.getenv('OPENAI_PROXY_URL')
        else:
            self.proxy_url = openai_proxy_url

        self.path = path
        self.max_completion_tokens = max_completion_tokens
        self.logger.warning(
            f'Max Completion tokens for {path} is :{max_completion_tokens}')
        self.is_chat = is_chat

    def generate(self,
                 inputs: List[PromptType],
                 max_out_len: int = 512,
                 temperature: float = 0.7,
                 **kwargs) -> List[str]:
        """Generate results given a list of inputs.

        Args:
            inputs (List[PromptType]): A list of strings or PromptDicts.
                The PromptDict should be organized in OpenCompass'
                API format.
            max_out_len (int): The maximum length of the output.
            temperature (float): What sampling temperature to use,
                between 0 and 2. Higher values like 0.8 will make the output
                more random, while lower values like 0.2 will make it more
                focused and deterministic. Defaults to 0.7.

        Returns:
            List[str]: A list of generated strings.
        """
        if self.temperature is not None:
            temperature = self.temperature

        with ThreadPoolExecutor() as executor:
            results = list(
                executor.map(self._generate, inputs,
                             [max_out_len] * len(inputs),
                             [temperature] * len(inputs)))
        return results

    def _generate(self, input: PromptType, max_out_len: int,
                  temperature: float) -> str:
        """Generate results given a list of inputs.

        Args:
            inputs (PromptType): A string or PromptDict.
                The PromptDict should be organized in OpenCompass'
                API format.
            max_out_len (int): The maximum length of the output.
            temperature (float): What sampling temperature to use,
                between 0 and 2. Higher values like 0.8 will make the output
                more random, while lower values like 0.2 will make it more
                focused and deterministic.

        Returns:
            str: The generated string.
        """
        assert isinstance(input, (str, PromptList))

        # max num token for gpt-3.5-turbo is 4097
        # Most models' token limits are above 32k
        context_window = 32768
        if '32k' in self.path:
            context_window = 32768
        elif '16k' in self.path:
            context_window = 16384
        elif 'gpt-4' in self.path:
            context_window = 8192
        elif 'gpt-3.5' in self.path:
            context_window = 4097

        # will leave 100 tokens as prompt buffer, triggered if input is str
        if isinstance(input, str) and self.mode != 'none':
            context_window = self.max_seq_len
            input = self.bin_trim(input, context_window - 100 - max_out_len)

        if isinstance(input, str):
            messages = [{'role': 'user', 'content': input}]
        else:
            messages = []
            for item in input:
                msg = {'content': item['prompt']}
                if item['role'] == 'HUMAN':
                    msg['role'] = 'user'
                elif item['role'] == 'BOT':
                    msg['role'] = 'assistant'
                elif item['role'] == 'SYSTEM':
                    msg['role'] = 'system'
                messages.append(msg)

        # Examples of messages:
        #   [{'role': 'user', 'content': 'Say this is a test'}]
        #   [{'role': 'system', 'content': 'You are a helpful assistant.'}, {'role': 'user', 'content': 'Hi, who are you?'}, {'role': 'assistant', 'content': 'I am AI assistant.'}]

        # Hold out 100 tokens due to potential errors in tiktoken calculation
        max_out_len = min(
            max_out_len, context_window - self.get_token_len(str(input)) - 100)
        if max_out_len <= 0:
            return ''
        max_num_retries = 0
        while max_num_retries < self.retry:
            self.wait()

            with Lock():
                if len(self.invalid_keys) == len(self.keys):
                    raise RuntimeError('All keys have insufficient quota.')

                # find the next valid key
                while True:
                    self.key_ctr += 1
                    if self.key_ctr == len(self.keys):
                        self.key_ctr = 0

                    if self.keys[self.key_ctr] not in self.invalid_keys:
                        break

                key = self.keys[self.key_ctr]

            header = {
                'Authorization': f'Bearer {key}',
                'content-type': 'application/json',
                'api-key': key,
            }

            if self.orgs:
                with Lock():
                    self.org_ctr += 1
                    if self.org_ctr == len(self.orgs):
                        self.org_ctr = 0
                header['OpenAI-Organization'] = self.orgs[self.org_ctr]

            try:
                if self.is_chat:
                    data = dict(
                        model=self.path,
                        messages=messages,
                        max_tokens=max_out_len,
                        n=1,
                        logprobs=self.logprobs,
                        top_logprobs=self.top_logprobs,
                        stop=None,
                        temperature=temperature,
                    )
                else:
                    # TODO: This is a temporary solution for non-chat models.
                    input_prompts = []
                    for msg in messages:
                        input_prompts.append(msg['content'])

                    data = dict(
                        model=self.path,
                        prompt='\n'.join(input_prompts),
                        max_tokens=max_out_len,
                        temperature=temperature,
                    )

                def remove_none_val(input_d: dict):
                    return {k: v for k, v in input_d.items() if v is not None}
                data = remove_none_val(data)

                raw_response = requests.post(self.url,
                                             headers=header,
                                             data=json.dumps(data))

            except requests.ConnectionError:
                self.logger.error('Got connection error, retrying...')
                continue
            try:
                response = raw_response.json()
            except requests.JSONDecodeError:
                self.logger.error('JsonDecode error, got',
                                  str(raw_response.content))
                continue
            self.logger.debug(str(response))
            try:
                if self.logprobs:
                    return response['choices']
                else:
                    if self.is_chat:
                        return response['choices'][0]['message']['content'].strip()
                    else:
                        return response['choices'][0]['text'].strip()
            except KeyError:
                if 'error' in response:
                    if response['error']['code'] == 'rate_limit_exceeded':
                        time.sleep(10)
                        self.logger.warn('Rate limit exceeded, retrying...')
                        continue
                    elif response['error']['code'] == 'insufficient_quota':
                        self.invalid_keys.add(key)
                        self.logger.warn(f'insufficient_quota key: {key}')
                        continue
                    elif response['error']['code'] == 'invalid_prompt':
                        self.logger.warn('Invalid prompt:', str(input))
                        return ''
                    elif response['error']['type'] == 'invalid_prompt':
                        self.logger.warn('Invalid prompt:', str(input))
                        return ''

                    self.logger.error('Find error message in response: ',
                                      str(response['error']))
            max_num_retries += 1

        raise RuntimeError('Calling OpenAI failed after retrying for '
                           f'{max_num_retries} times. Check the logs for '
                           'details.')

    def get_token_len(self, prompt: str) -> int:
        """Get lengths of the tokenized string. Only English and Chinese
        characters are counted for now. Users are encouraged to override this
        method if more accurate length is needed.

        Args:
            prompt (str): Input string.

        Returns:
            int: Length of the input tokens
        """
        if self.tokenizer_path:
            try:
                enc = self.tiktoken.encoding_for_model(self.tokenizer_path)
                return len(enc.encode(prompt))
            except Exception:
                from transformers import AutoTokenizer
                if self.hf_tokenizer is None:
                    self.hf_tokenizer = AutoTokenizer.from_pretrained(
                        self.tokenizer_path)
                return len(self.hf_tokenizer(prompt).input_ids)
        else:
            enc = self.tiktoken.encoding_for_model(self.path)
            return len(enc.encode(prompt))

    def bin_trim(self, prompt: str, num_token: int) -> str:
        """Get a suffix of prompt which is no longer than num_token tokens.

        Args:
            prompt (str): Input string.
            num_token (int): The upper bound of token numbers.

        Returns:
            str: The trimmed prompt.
        """
        token_len = self.get_token_len(prompt)
        if token_len <= num_token:
            return prompt
        pattern = re.compile(r'[\u4e00-\u9fa5]')
        if pattern.search(prompt):
            words = list(jieba.cut(prompt, cut_all=False))
            sep = ''
        else:
            words = prompt.split(' ')
            sep = ' '

        l, r = 1, len(words)
        while l + 2 < r:
            mid = (l + r) // 2
            if self.mode == 'front':
                cur_prompt = sep.join(words[-mid:])
            elif self.mode == 'mid':
                cur_prompt = sep.join(words[:mid]) + sep.join(words[-mid:])
            elif self.mode == 'rear':
                cur_prompt = sep.join(words[:mid])

            if self.get_token_len(cur_prompt) <= num_token:
                l = mid  # noqa: E741
            else:
                r = mid

        if self.mode == 'front':
            prompt = sep.join(words[-l:])
        elif self.mode == 'mid':
            prompt = sep.join(words[:l]) + sep.join(words[-l:])
        elif self.mode == 'rear':
            prompt = sep.join(words[:l])
        return prompt


class OpenAISDK(OpenAI):

    def __init__(self,
                 path: str = 'gpt-3.5-turbo',
                 max_seq_len: int = 4096,
                 query_per_second: int = 1,
                 rpm_verbose: bool = False,
                 retry: int = 2,
                 key: Union[str, List[str]] = 'ENV',
                 org: Optional[Union[str, List[str]]] = None,
                 meta_template: Optional[Dict] = None,
                 openai_api_base: str = OPENAI_API_BASE,
                 openai_proxy_url: Optional[str] = None,
                 mode: str = 'none',
                 logprobs: Optional[bool] = False,
                 top_logprobs: Optional[int] = None,
                 temperature: Optional[float] = 0.0,
                 tokenizer_path: Optional[str] = None,
                 extra_body: Optional[Dict] = None):
        super().__init__(path, max_seq_len, query_per_second, rpm_verbose,
                         retry, key, org, meta_template, openai_api_base,
                         openai_proxy_url, mode, logprobs, top_logprobs,
                         temperature, tokenizer_path, extra_body)
        from openai import OpenAI

        if self.proxy_url is None:
            self.openai_client = OpenAI(base_url=openai_api_base, api_key=key)
        else:
            proxies = {
                'http://': self.proxy_url,
                'https://': self.proxy_url,
            }

            self.openai_client = OpenAI(
                base_url=openai_api_base,
                api_key=key,
                http_client=httpx.Client(proxies=proxies))

    def _generate(self,
                  input: Union[str, PromptList],
                  max_out_len: int,
                  temperature: float) -> str:
        from openai import APIStatusError, BadRequestError
        assert isinstance(input, (str, PromptList))

        # max num token for gpt-3.5-turbo is 4097
        # Most models' token limits are above 32k
        context_window = 32768
        if '32k' in self.path:
            context_window = 32768
        elif '16k' in self.path:
            context_window = 16384
        elif 'gpt-4' in self.path:
            context_window = 8192
        elif 'gpt-3.5' in self.path:
            context_window = 4097

        # will leave 100 tokens as prompt buffer, triggered if input is str
        if isinstance(input, str) and self.mode != 'none':
            context_window = self.max_seq_len
            input = self.bin_trim(input, context_window - 100 - max_out_len)

        if isinstance(input, str):
            messages = [{'role': 'user', 'content': input}]
        else:
            messages = []
            for item in input:
                msg = {'content': item['prompt']}
                if item['role'] == 'HUMAN':
                    msg['role'] = 'user'
                elif item['role'] == 'BOT':
                    msg['role'] = 'assistant'
                elif item['role'] == 'SYSTEM':
                    msg['role'] = 'system'
                messages.append(msg)

        # Hold out 100 tokens due to potential errors in tiktoken calculation
        # try:
        #     max_out_len = min(
        #         max_out_len,
        #         context_window - self.get_token_len(str(input)) - 100)
        # except KeyError:
        #     max_out_len = max_out_len
        # if max_out_len <= 0:
        #     return ''

        num_retries = 0
        while num_retries < self.retry:
            self.wait()
            try:
                responses = self.openai_client.chat.completions.create(
                    model=self.path,
                    max_tokens=max_out_len,
                    n=1,
                    temperature=self.temperature,
                    messages=messages,
                    extra_body=self.extra_body,
                )
                return responses.choices[0].message.content
            except Exception as e:
                self.logger.error(e)
            num_retries += 1
        raise RuntimeError('Calling OpenAI API failed after retrying for '
                           f'{self.retry} times. Check the logs for details.')


@MODELS.register_module()
class AsyncOpenAI(OpenAI):
    """
    Async version of OpenAI model.

    Args:
        path (str): The name of OpenAI's model.
        max_seq_len (int): The maximum allowed sequence length of a model.
            Note that the length of prompt + generated tokens shall not exceed
            this value. Defaults to 2048.
        query_per_second (int): The maximum queries allowed per second
            between two consecutive calls of the API. Defaults to 1.
        retry (int): Number of retires if the API call fails. Defaults to 2.
        key (str or List[str]): OpenAI key(s). In particular, when it
            is set to "ENV", the key will be fetched from the environment
            variable $OPENAI_API_KEY, as how openai defaults to be. If it's a
            list, the keys will be used in round-robin manner. Defaults to
            'ENV'.
        org (str or List[str], optional): OpenAI organization(s). If not
            specified, OpenAI uses the default organization bound to each API
            key. If specified, the orgs will be posted with each request in
            round-robin manner. Defaults to None.
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
        openai_api_base (str): The base url of OpenAI's API. Defaults to
            'https://api.openai.com/v1/chat/completions'.
        mode (str, optional): The method of input truncation when input length
            exceeds max_seq_len. 'front','mid' and 'rear' represents the part
            of input to truncate. Defaults to 'none'.
        temperature (float, optional): What sampling temperature to use.
            If not None, will override the temperature in the `generate()`
            call. Defaults to None.
    """

    def __init__(self, *args, **kwargs):

        super(AsyncOpenAI, self).__init__(*args, **kwargs)

    def generate(self,
                 inputs: List[PromptType],
                 max_out_len: int = 512,
                 temperature: float = 0.7,
                 **kwargs) -> List[str]:

        results = []

        if len(inputs) == 0:
            self.logger.error('Got empty input list in generate()')
            return results

        if self.temperature is not None:
            temperature = self.temperature

        inputs_batch = []

        for input_sample in inputs:

            if isinstance(input_sample, str):
                messages = [{'role': 'user', 'content': input_sample}]
            else:
                messages = []
                for item in input_sample:
                    msg = {'content': item['prompt']}
                    if item['role'] == 'HUMAN':
                        msg['role'] = 'user'
                    elif item['role'] == 'BOT':
                        msg['role'] = 'assistant'
                    elif item['role'] == 'SYSTEM':
                        msg['role'] = 'system'
                    messages.append(msg)

            inputs_batch.append(messages)

        infer_config = dict(
            temperature=temperature,
            max_tokens=max_out_len,
        )
        self.logger.info(f'>>infer_config: {infer_config}')
        self.logger.info(f'>>len of inputs_batch: {len(inputs_batch)}')

        request_config = XRequestConfig(**infer_config)

        resp_list = asyncio.run(
            OpenAIClientUtil.call_openai_batched(
                model_type=self.path,
                messages_batch=inputs_batch,
                request_config=request_config,
                base_url=self.url,
                is_chat=self.is_chat,
            )
        )

        self.logger.info(f'>> resp_list len: {len(resp_list)}')
        self.logger.info(f'>> resp_list[0]: {resp_list[0]}')

        return resp_list

    def get_token_len(self, prompt: str) -> int:
        """Get lengths of the tokenized string. Only English and Chinese
        characters are counted for now. Users are encouraged to override this
        method if more accurate length is needed.

        Args:
            prompt (str): Input string.

        Returns:
            int: Length of the input tokens
        """
        english_parts = re.findall(r'[A-Za-z0-9]+', prompt)
        chinese_parts = re.findall(r'[\u4e00-\u9FFF]+', prompt)

        # Count English words
        english_count = sum(len(part.split()) for part in english_parts)

        # Count Chinese words
        chinese_count = sum(len(part) for part in chinese_parts)

        return english_count + chinese_count


@MODELS.register_module()
class OpenAIExtra(OpenAI):
    """
    Model wrapper around OpenAI's models with extra features.
    Args:
        path (str): The name of OpenAI's model.
        max_seq_len (int): The maximum allowed sequence length of a model.
            Note that the length of prompt + generated tokens shall not exceed
            this value. Defaults to 2048.
        query_per_second (int): The maximum queries allowed per second
            between two consecutive calls of the API. Defaults to 1.
        retry (int): Number of retires if the API call fails. Defaults to 2.
        key (str or List[str]): OpenAI key(s). In particular, when it
            is set to "ENV", the key will be fetched from the environment
            variable $OPENAI_API_KEY, as how openai defaults to be. If it's a
            list, the keys will be used in round-robin manner. Defaults to
            'ENV'.
        org (str or List[str], optional): OpenAI organization(s). If not
            specified, OpenAI uses the default organization bound to each API
            key. If specified, the orgs will be posted with each request in
            round-robin manner. Defaults to None.
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
        openai_api_base (str): The base url of OpenAI's API. Defaults to
            'https://api.openai.com/v1/chat/completions'.
        mode (str, optional): The method of input truncation when input length
            exceeds max_seq_len. 'front','mid' and 'rear' represents the part
            of input to truncate. Defaults to 'none'.
        temperature (float, optional): What sampling temperature to use.
            If not None, will override the temperature in the `generate()`
            call. Defaults to None.
    """

    def __init__(self, *args, **kwargs):

        super(OpenAIExtra, self).__init__(*args, **kwargs)

    def get_token_len(self, prompt: str) -> int:
        """Get lengths of the tokenized string. Only English and Chinese
        characters are counted for now. Users are encouraged to override this
        method if more accurate length is needed.
        Args:
            prompt (str): Input string.
        Returns:
            int: Length of the input tokens
        """
        english_parts = re.findall(r'[A-Za-z0-9]+', prompt)
        chinese_parts = re.findall(r'[\u4e00-\u9FFF]+', prompt)

        # Count English words
        english_count = sum(len(part.split()) for part in english_parts)

        # Count Chinese words
        chinese_count = sum(len(part) for part in chinese_parts)

        return english_count + chinese_count
