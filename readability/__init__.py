import re, requests, yaml

from datetime import datetime

from lxml import html

MetaProps=yaml.safe_load("""
src:
- og:site_name
- twitter:site
lang:
- og:locale
title: 
- og:title
- twitter:title
description:
- og:description
- twitter:description
img:
- og:image
- twitter:image:src
""")

"""
- https://docs.aws.amazon.com/translate/latest/dg/what-is.html
- https://www.worldstandards.eu/other/tlds/
"""

Domains=Languages="de|es|fr|it|ru".split("|")

DefaultTags="p|h1|h2|h3|h4|h5|h6".split("|")

UTF8="utf-8"

MaxDescriptionSentences=2

ChunkSize=64

def simple_tag_matcher(tag):
    return lambda el: (str(el.tag)==tag and
                       has_text_content(el))

def has_text_content(el):
    return re.sub("\\s", "", str(el.text_content()))!=""

def format_lang(text):
    return re.split("\\-|\\_", text)[0]

def format_description(text, sz=MaxDescriptionSentences):
    def tokenise(text):
        tokens, token = [], ""
        for c in text:
            if (c==" " and
                token!="" and
                token[-1] in "?!."):
                tokens.append(token)
                token=""
            else:
                token+=c
        if token!="":
            tokens.append(token)
        return tokens
    tokens=tokenise(text)
    return " ".join(tokens[:sz])

"""
- all lxml xpath output converted to str() as is sometimes some kind of class
"""

def filter_lang(fn):
    def wrapped(doc):
        head=fn(doc)
        elements=doc.xpath("//html")
        if (elements!=[] and
            "lang" not in head):
            element=elements.pop()
            if "lang" in element.attrib:
                head["lang"]=format_lang(str(element.attrib["lang"]))
        return head
    return wrapped

def filter_title(fn):
    def wrapped(doc):
        head=fn(doc)
        elements=doc.xpath("//title")
        if (elements!=[] and
            "title" not in head):
            head["title"]=str(elements.pop().text_content())
        return head
    return wrapped

def filter_description(fn):
    def wrapped(doc):
        head=fn(doc)
        elements=doc.xpath("//meta[@name='description']")
        if (elements!=[] and
            "description" not in head):
            element=elements.pop()
            if "content" in element.attrib:
                head["description"]=format_description(str(element.attrib["content"]))
        return head
    return wrapped

def filter_src(fn):
    def wrapped(doc, props=["application-name",
                            "apple-mobile-web-app-title"]):
        attrs={str(el.attrib["name"]): str(el.attrib["content"])
               for el in doc.xpath("//meta")
               if ("name" in el.attrib and
                   "content" in el.attrib)}
        head=fn(doc)
        for prop in props:
            if (prop in attrs and
                "src" not in head):
                head["src"]=attrs[prop]
                break
        return head
    return wrapped

def filter_meta_props(fn):
    def format_value(k, v):
        if k=="lang":
            return format_lang(v)
        return v
    def wrapped(doc, props=MetaProps):
        attrs={str(el.attrib["property"]): str(el.attrib["content"])
               for el in doc.xpath("//meta")
               if ("property" in el.attrib and
                   "content" in el.attrib)}
        head=fn(doc)
        for key in props:
            for prop in props[key]:
                if (prop in attrs and
                    key not in head):
                    head[key]=format_value(key, attrs[prop])
                    break
        return head
    return wrapped

@filter_meta_props
@filter_lang
@filter_title
@filter_description
@filter_src
def init_head(doc):
    timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return {"timestamp": timestamp}

def add_ids(fn):
    def wrapped(doc, **kwargs):
        items=fn(doc, **kwargs)
        for i, item in enumerate(items):
            item["id"]=i
        return items
    return wrapped

class Counts(dict):
    @classmethod
    def create(self, body):
        counts=Counts()
        for item in body:
            tokens=re.split("\\s", item["text"])
            counts.setdefault(item["depth"], 0)
            counts[item["depth"]]+=len(tokens)
        return counts
    def __init__(self):
        dict.__init__(self)
    @property
    def weights(self):
        total=sum(self.values())
        return [(k, self[k]/total)
                for k in sorted(self.keys())]

@add_ids
def init_body(doc, matchers):
    def filter_text(el):
        return " ".join([tok
                         for tok in re.split("\\s", str(el.text_content()))
                         if tok!=''])
    def filter_body(doc):
        def filter_body(el, items, i=0):
            for matcher in matchers:
                if matcher(el):
                    tag=str(el.tag)
                    text=filter_text(el)
                    items.append({"tag": tag,
                                  "text": text,
                                  "depth": i})
                    break
            for child in el.getchildren():
                filter_body(child, items, i+1)
        items=[]
        filter_body(doc.xpath("//body").pop(), items)
        return items
    body=filter_body(doc)    
    weights=sorted(Counts.create(body).weights,
                   key=lambda x: x[1])
    if weights==[]:
        raise RuntimeError("no weights found")
    filterfn=lambda x: ((x["depth"] >= weights[-1][0]) and
                        (x["depth"] < weights[-1][0]+1))
    return [item for item in body
            if (re.sub("\\s", "", item["text"])!='' and
                filterfn(item))]

def finalise_head(fn):
    def absolute_img(head):
        if ("img" in head and
            not head["img"].startswith("http")):
            head["img"]="/".join(head["url"].split("/")[:3])+head["img"]
    def lang_from_url(head, domains=Domains):
        if "lang" not in head:
            lang=head["url"].split("/")[2].split(".")[-1]
            if lang in domains:
                head["lang"]=lang
    def wrapped(url):
        resp=fn(url)
        head=resp["head"]
        head["url"]=url
        for modifier in [absolute_img,
                         lang_from_url]:
            modifier(head)
        return resp
    return wrapped

def finalise_body(fn):
    def init_phrases(tokens):
        groups, group = [], []
        for token in tokens:
            group.append(token)
            if (token[-1] in "?|." and
                group!=[]):
                groups.append(group)
                group=[]
        if group!=[]:
            groups.append(group)
        return groups
    def init_chunks(phrases, n=ChunkSize):
        chunks, chunk = [], []
        for phrase in phrases:
            if len(chunk) > n:
                chunks.append(chunk)
                chunk=[]
            chunk+=phrase
        if chunk!=[]:
            chunks.append(chunk)
        return chunks
    def tokenise(text):
        tokens=[tok for tok in re.split("\\s", text)
                if tok!='']
        phrases=init_phrases(tokens)
        chunks=init_chunks(phrases)
        return [" ".join(chunk)
                for chunk in chunks]
    def wrapped(url):
        struct, body, count = fn(url), [], 1
        for item in struct["body"]:
            for chunk in tokenise(item["text"]):
                moditem=dict(item)
                moditem["text"]=chunk
                moditem["id"]=count
                body.append(moditem)
                count+=1
        struct["body"]=body
        return struct
    return wrapped

"""
- https://requests.readthedocs.io/en/master/user/quickstart/#response-content
- https://stackoverflow.com/questions/44203397/python-requests-get-returns-improperly-decoded-text-instead-of-utf-8
"""

@finalise_head
@finalise_body
def fetch(url,
          tags=DefaultTags):
    resp=requests.get(url)
    if resp.status_code!=200:
        raise RuntimeError("page server returned HTTP %i" % resp.status_code)
    # https://stackoverflow.com/a/52615216/124179
    if (resp.encoding!=UTF8 and
        resp.apparent_encoding==UTF8):        
        resp.encoding=resp.apparent_encoding
    doc=html.fromstring(resp.text)
    head=init_head(doc)
    head["encoding"]=resp.encoding
    matchers=[simple_tag_matcher(tag=tag)
              for tag in tags]
    body=init_body(doc,
                   matchers=matchers)
    return {"head": head,
            "body": body}

if __name__=="__main__":
    try:
        import sys
        if len(sys.argv) < 2:
            raise RuntimeError("Please enter URL")
        url=sys.argv[1]
        resp=fetch(url)
        print (yaml.safe_dump(resp,
                              default_flow_style=False,
                              allow_unicode=True))
    except RuntimeError as error:
        print ("Error: %s" % str(error))
        
