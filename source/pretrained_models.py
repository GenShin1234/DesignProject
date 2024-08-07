# 使用预训练模型
from transformers import PegasusTokenizer, PegasusForConditionalGeneration
from transformers import T5Tokenizer, T5ForConditionalGeneration, AdamW
from transformers import BartTokenizer, BartForConditionalGeneration
from settings import *
from utils import GetRouge, CountFiles
import os
from torch.utils.data.dataset import TensorDataset
from torch.utils.data.dataloader import DataLoader
from torch.nn.modules.module import Module

current_model = "" # 存储当前使用的模型类型

# 将输入文本和摘要转换为 PyTorch 张量，用于模型训练和评估
def ToTensor(texts, summaries, tokenizer):
    task_prefix = "summarize: "
    # 为每个输入文本添加一个任务前缀 进行编码
    encoding = tokenizer([task_prefix + sequence for sequence in texts],
                         padding='longest',  # 对所有序列进行填充，使得最长的序列长度一致
                         max_length=SOURCE_THRESHOLD,  # 序列长度限制
                         truncation=True,  # 序列超出 max_length，则进行截断
                         return_tensors="pt")  # 返回 PyTorch 张量
    input_ids, attention_mask = encoding.input_ids, encoding.attention_mask
    # 对 summaries 中的每个摘要进行编码
    target_encoding = tokenizer(summaries,
                                padding='longest',
                                max_length=SUMMARY_THRESHOLD,
                                truncation=True)
    labels = target_encoding.input_ids
    # 填充的 ID 不应该参与损失计算
    labels = [(i if i != tokenizer.pad_token_id else -100) for i in labels]
    labels = torch.tensor(labels) # 将标签列表转换为 PyTorch 张量

    return TensorDataset(input_ids, attention_mask, labels)

# 微调预训练模型
"""
读取训练和验证数据集，并使用 DataLoader 进行批量处理，然后进行模型训练和验证。
"""
def FineTune(net: Module, tokenizer):
    '''微调'''

    tset_texts = []
    tset_summaries = []
    vset_texts = []
    vset_summaries = []
    # 计算训练和验证数据集的大小
    tset_len = CountFiles(DATA_DIR + "new_train")
    vset_len = CountFiles(DATA_DIR + "new_val")
    for i in range(tset_len):
        text, summary = ReadJson(i, DATA_DIR + "new_train")
        tset_texts.append(text)
        tset_summaries.append(summary)
    for i in range(vset_len):
        text, summary = ReadJson(i, DATA_DIR + "new_val")
        vset_texts.append(text)
        vset_summaries.append(summary)
    print("训练数据已读入内存...")
    # 使用 ToTensor 函数将训练和验证数据转换为张量格式，然后使用 DataLoader 类将数据打包成批处理格式。
    train_iter = DataLoader(
        ToTensor(tset_texts, tset_summaries, tokenizer),
        batch_size=BATCH_SZIE,
        shuffle=True,
        num_workers=4
    )
    val_iter = DataLoader(
        ToTensor(vset_texts, vset_summaries, tokenizer),
        batch_size=BATCH_SZIE,
        shuffle=False,
        num_workers=4
    )

    print("minibatch已生成...")

    print("开始训练模型...")
    opt = AdamW(net.parameters()) # 用于更新模型参数
    from tqdm import tqdm
    import time
    min_loss = 10
    for epoch in range(EPOCHS):
        train_loss = []
        val_loss = []
        net.train()
        for batch in tqdm(train_iter):
            input_ids, attention_mask, labels = [x.to(DEVICE) for x in batch]
            l = net(input_ids=input_ids, attention_mask=attention_mask, labels=labels).loss
            l.backward()
            opt.step()
            opt.zero_grad()
            with torch.no_grad():
                train_loss.append(l.item())
        # 在训练一个 epoch 后，清空 GPU 缓存，将模型设置为评估模式
        torch.cuda.empty_cache()
        net.eval()
        with torch.no_grad():  # 使用 torch.no_grad() 来避免计算梯度，从而节省 GPU 资源
            for batch in tqdm(val_iter):
                input_ids, attention_mask, labels = [x.to(DEVICE) for x in batch]
                l = net(input_ids=input_ids, attention_mask=attention_mask, labels=labels).loss
                val_loss.append(l.item())

        if sum(val_loss) < min_loss:
            min_loss = sum(val_loss)
            torch.save(net.state_dict(), PARAM_DIR + str(int(time.time())) + "_GRU.param")
            print(f"saved net with val_loss:{min_loss}")

        print(f"{epoch + 1}: train_loss:{sum(train_loss)};val_loss:{sum(val_loss)}")


def TestOneSeq(net, tokenizer, text, target=None):
    """生成单个样本的摘要"""
    torch.cuda.empty_cache()
    net.eval()

    text = str(text).replace('\n', '')
    input_tokenized = tokenizer.encode(
        text,
        truncation=True,
        return_tensors="pt",
        max_length=SOURCE_THRESHOLD
    ).to(DEVICE)

    # if current_model == "t5":
    #     summary_task = torch.tensor([[21603, 10]]).to(DEVICE)
    #     input_tokenized = torch.cat([summary_task, input_tokenized], dim=-1).to(DEVICE)

    # 使用模型的 generate 方法生成摘要
    summary_ids = net.generate(input_tokenized,
                               num_beams=NUM_BEAMS, # 用于束搜索的束数量
                               no_repeat_ngram_size=3, # 防止生成重复的 n-gram
                               min_length=MIN_LEN,  # 生成摘要的最小长度
                               max_length=MAX_LEN,
                               early_stopping=True) # 生成器会在遇到一个 EOS 标记时停止生成
    # 从生成的 summary_ids 中提取输出摘要。tokenizer.decode 方法将 Token ID 转换回文本。
    output = [tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=False) for g in summary_ids]
    score = -1
    if target is not None:
        score = GetRouge(output[0], target) # 输出摘要与目标摘要之间的 ROUGE 分数
    return output[0], score


# 加载 T5 模型和分词器
def GetTextSum_T5(name):
    tokenizer = T5Tokenizer.from_pretrained(PARAM_DIR + name)
    net = T5ForConditionalGeneration.from_pretrained(PARAM_DIR + name)
    print(f"{name} 加载完毕")
    return net.to(DEVICE), tokenizer


# 函数加载 BART 模型和分词器。
def GetTextSum_BART():
    tokenizer = BartTokenizer.from_pretrained(PARAM_DIR + "bart", output_past=True)
    net = BartForConditionalGeneration.from_pretrained(PARAM_DIR + "bart", output_past=True)
    print("bart 加载完毕")
    return net.to(DEVICE), tokenizer

# 加载 Pegasus 模型和分词器
def GetTextSum_Pegasus():
    tokenizer = PegasusTokenizer.from_pretrained(PARAM_DIR + "pegasus")
    net = PegasusForConditionalGeneration.from_pretrained(PARAM_DIR + "pegasus")
    # net = PegasusForConditionalGeneration.from_pretrained("pegasus", token='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')
    print("pegasus 加载完毕")
    return net.to(DEVICE), tokenizer

# 根据名称加载不同的预训练模型和分词器
def GetPModel(name: str):
    global current_model
    name = name.lower()
    print("正在加载模型")
    if "t5" in name:
        current_model = "t5"
        return GetTextSum_T5(name)
    elif name == "bart":
        return GetTextSum_BART()
    elif name == "pegasus":
        current_model = "pegasus"
        return GetTextSum_Pegasus()
    else:
        raise Exception("该模型未实现！")

# 从给定的目录中读取 JSON 文件，并返回文本和摘要
def ReadJson(i, dir, test=False):
    """读取单个json文件（一个样本）"""
    import json

    js_data = json.load(open(os.path.join(dir, f"{i}.json"), encoding="utf-8"))
    if test:
        return js_data["text"]
    return js_data["text"], js_data["summary"]

# 生成一个 submission 包含模型的预测摘要
def GenSub(net, tokenizer, param_path=None):
    """生成submission.csv"""
    import csv
    from tqdm import tqdm

    if param_path is not None:
        net.load_state_dict(torch.load(param_path))
    res = [] # 存储生成的摘要
    for i in tqdm(range(1000)):
        text = ReadJson(i, DATA_DIR + "new_test", True)
        summary = TestOneSeq(net, tokenizer, text)[0]
        summary = summary.replace('\t ', '\t')
        res.append([str(i), summary])

    with open(os.path.join(DATA_DIR, 'submission.csv'), 'w+', newline="", encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile, delimiter="\t")
        writer.writerows(res)


if __name__ == '__main__':
    net, tokenizer = GetPModel("bart")
    res = tokenizer(
        ["hello world", "hi"],
        return_tensors="pt",
        padding='longest',
        max_length=MAX_LEN,
        truncation=True,
    )
    # print(res)

    print(TestOneSeq(
        net, tokenizer,
        "one-third of phone users would definitely upgrade to a facebook phone - and 73 % think the phone is a ` good idea ' . news of the phone emerged this week , with sources claiming that facebook had hired ex-apple engineers to work on an ` official ' facebook phone . facebook has made several ventures into the mobile market before in partnership with manufacturers such as htc and inq - but a new phone made by ex-apple engineers is rumoured to be in production . the previous ` facebook phone ' - inq 's cloud touch - puts all of your newsfeeds , pictures and other information on a well thought-out homescreen centred around facebook . it 's not the first facebook phone to hit . the market -- the social network giant has previously partnered with inq . and htc to produce facebook-oriented handsets , including phones with a . built-in ` like ' button . details of the proposed phone are scant , but facebook is already making moves into the mobile space with a series of high-profile app acquisitions . after its $ 1 billion purchase of instagram , the social network bought location-based social app glancee and photo-sharing app lightbox . facebook 's smartphone apps have also seen constant and large-scale redesigns , with adverts more prominent with the news feed . the handset is rumoured to be set for a 2013 release . it could be a major hit -- a flash poll of 968 people conducted by myvouchercodes found that 32 % of phone users would upgrade as soon as it became available . the key to its success could be porting apps to mobile -- something facebook is already doing . separate camera and chat apps already separate off some site functions , and third-party apps will shortly be available via a facebook app store . of those polled , 57 % hoped that it would be cheaper than an iphone -- presumably supported by facebook 's advertising . those polled were then asked why they would choose to purchase a facebook phone , if and when one became available , and were asked to select all reasons that applied to them from a list of possible answers . would you ` upgrade ' to a facebook phone ? would you ` upgrade ' to a facebook phone ? now share your opinion . the top five reasons were as follows : . 44 % of people liked the idea of having their mobile phone synced with their facebook account , whilst 41 % said they wanted to be able to use facebook apps on their smartphone . mark pearson , chairman of myvouchercodes.co.uk , said , ` it will be quite exciting to see the first facebook phone when it 's released next year . '",
        "poll of 968 phone users in uk .   32 % said they would definitely upgrade to a facebook phone .   users hope it might be cheaper than iphone . "
    ))
    GenSub(net, tokenizer)
    #
    # opt = AdamW(net.parameters())
    # opt.step()

    # FineTune(net, tokenizer)

    # with open("1.txt", "w+") as f:
    #     f.write(str(net))
